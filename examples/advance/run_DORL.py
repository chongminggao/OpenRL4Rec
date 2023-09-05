import argparse
import functools
import os
import pprint
import sys
import traceback
import pickle
import random

import numpy as np
import torch

sys.path.extend([".", "./examples", "./src", "./src/DeepCTR-Torch", "./src/tianshou"])

from policy.policy_utils import get_args_all, prepare_dir_log, prepare_user_model, prepare_test_envs, setup_state_tracker

# os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

from core.collector.collector_set import CollectorSet
from core.evaluation.evaluator import Evaluator_Feat, Evaluator_Coverage_Count, Evaluator_User_Experience, save_model_fn
from core.evaluation.loggers import LoggerEval_Policy
from core.util.data import get_common_args, get_val_data, get_training_item_domination, get_item_similarity, get_item_popularity, get_true_env
from core.collector.collector import Collector
from core.policy.a2c import A2CPolicy_withEmbedding
from core.trainer.onpolicy import onpolicy_trainer
from environments.Simulated_Env.penalty_ent_exp import PenaltyEntExpSimulatedEnv

from tianshou.data import VectorReplayBuffer
from tianshou.env import DummyVectorEnv
from tianshou.utils.net.common import ActorCritic, Net
from tianshou.utils.net.discrete import Actor, Critic

# from util.upload import my_upload
import logzero
from logzero import logger

try:
    import envpool
except ImportError:
    envpool = None


def get_args_DORL():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, default="MOPO")
    parser.add_argument('--vf-coef', type=float, default=0.5)
    parser.add_argument('--ent-coef', type=float, default=0.0)
    parser.add_argument('--max-grad-norm', type=float, default=None)
    parser.add_argument('--gae-lambda', type=float, default=1.)
    parser.add_argument('--rew-norm', action="store_true", default=False)

    # Env
    parser.add_argument('--is_exposure_intervention', dest='use_exposure_intervention', action='store_true')
    parser.add_argument('--no_exposure_intervention', dest='use_exposure_intervention', action='store_false')
    parser.set_defaults(use_exposure_intervention=False)

    parser.add_argument("--version", type=str, default="v1")
    parser.add_argument('--tau', default=0, type=float)
    parser.add_argument('--gamma_exposure', default=10, type=float)

    parser.add_argument('--lambda_entropy', default=5, type=float)

    parser.add_argument("--read_message", type=str, default="UM")
    parser.add_argument("--message", type=str, default="DORL")

    args = parser.parse_known_args()[0]
    return args

def prepare_train_envs(args, ensemble_models):
    env, env_task_class, kwargs_um = get_true_env(args)

    entropy_dict = dict()
    # if 0 in args.entropy_window:
    #     entropy_path = os.path.join(ensemble_models.Entropy_PATH, "user_entropy.csv")
    #     entropy = pd.read_csv(entropy_path)
    #     entropy.set_index("user_id", inplace=True)
    #     entropy_mat_0 = entropy.to_numpy().reshape([-1])
    #     entropy_dict.update({"on_user": entropy_mat_0})
    if len(set(args.entropy_window) - set([0])):
        savepath = os.path.join(ensemble_models.Entropy_PATH, "map_entropy.pickle")
        map_entropy = pickle.load(open(savepath, 'rb'))
        entropy_dict.update({"map": map_entropy})

    entropy_set = set(args.entropy_window)
    entropy_min = 0
    entropy_max = 0
    if len(entropy_set):
        for entropy_term in entropy_set:
            entropy_min += min([v for k, v in entropy_dict["map"].items() if len(k) == entropy_term])
            entropy_max += max([v for k, v in entropy_dict["map"].items() if len(k) == entropy_term])

    with open(ensemble_models.PREDICTION_MAT_PATH, "rb") as file:
        predicted_mat = pickle.load(file)

    alpha_u, beta_i = None, None  ## TODO

    kwargs = {
        "ensemble_models": ensemble_models,
        "env_task_class": env_task_class,
        "task_env_param": kwargs_um,
        "task_name": args.env,
        "predicted_mat": predicted_mat,

        "version": args.version,
        "tau": args.tau,
        "use_exposure_intervention": args.use_exposure_intervention,
        "gamma_exposure": args.gamma_exposure,
        "alpha_u": alpha_u,
        "beta_i": beta_i,
        "entropy_dict": entropy_dict,
        "entropy_window": args.entropy_window,
        "lambda_entropy": args.lambda_entropy,
        "step_n_actions": max(args.entropy_window) if len(args.entropy_window) else 0,
        "entropy_min": entropy_min,
        "entropy_max": entropy_max,
    }

    train_envs = DummyVectorEnv(
        [lambda: PenaltyEntExpSimulatedEnv(**kwargs) for _ in range(args.training_num)])
    
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)

    return env, train_envs


def setup_policy_model(args, state_tracker, train_envs, test_envs_dict):
    if args.cpu:
        args.device = "cpu"
    else:
        args.device = torch.device("cuda:{}".format(args.cuda) if torch.cuda.is_available() else "cpu")

    # model
    net = Net(args.state_dim, hidden_sizes=args.hidden_sizes, device=args.device)
    actor = Actor(net, args.action_shape, device=args.device).to(args.device)
    critic = Critic(net, device=args.device).to(args.device)
    optim_RL = torch.optim.Adam(ActorCritic(actor, critic).parameters(), lr=args.lr)
    optim_state = torch.optim.Adam(state_tracker.parameters(), lr=args.lr)
    optim = [optim_RL, optim_state]

    dist = torch.distributions.Categorical
    policy = A2CPolicy_withEmbedding(
        actor,
        critic,
        optim,
        dist,
        state_tracker=state_tracker,
        discount_factor=args.gamma,
        gae_lambda=args.gae_lambda,
        vf_coef=args.vf_coef,
        ent_coef=args.ent_coef,
        max_grad_norm=args.max_grad_norm,
        reward_normalization=args.rew_norm,
        action_space=args.action_shape,
        action_bound_method="",  # not clip
        action_scaling=False
    )
    policy.set_eps(args.eps)

    # Prepare the collectors and logs
    train_collector = Collector(
        policy, train_envs,
        VectorReplayBuffer(args.buffer_size, len(train_envs)),
        preprocess_fn=state_tracker.build_state,
        exploration_noise=args.exploration_noise,
    )
    # test_collector = Collector(
    #     policy, test_envs_dict,
    #     VectorReplayBuffer(args.buffer_size, len(test_envs)),
    #     preprocess_fn=state_tracker.build_state,
    #     exploration_noise=args.exploration_noise,
    # )
    policy.set_collector(train_collector)

    test_collector_set = CollectorSet(policy, test_envs_dict, args.buffer_size, args.test_num,
                                      preprocess_fn=state_tracker.build_state,
                                      exploration_noise=args.exploration_noise,
                                      force_length=args.force_length)

    return policy, train_collector, test_collector_set, optim


def learn_policy(args, env, policy, train_collector, test_collector_set, state_tracker, optim, MODEL_SAVE_PATH,
                 logger_path):
    # log
    # log_path = os.path.join(args.logdir, args.env, 'a2c')
    # writer = SummaryWriter(log_path)
    # logger1 = TensorboardLogger(writer)
    # def save_best_fn(policy):
    #     torch.save(policy.state_dict(), os.path.join(log_path, 'policy.pth'))

    # env = test_collector_set.env
    df_val, df_user_val, df_item_val, list_feat = get_val_data(args.env)
    item_feat_domination = get_training_item_domination(args.env)
    item_similarity, item_popularity = get_item_similarity(args.env), get_item_popularity(args.env)

    # set metrics and related evaluator
    metrics = ['len_tra', 'R_tra', 'ctr', 'CV', 'CV_turn', 'ifeat_', 'Diversity', 'Novelty']

    policy.callbacks = [
        Evaluator_Feat(test_collector_set, df_item_val, args.need_transform, item_feat_domination,
                                lbe_item=env.lbe_item if args.need_transform else None, top_rate=args.top_rate, draw_bar=args.draw_bar),
        Evaluator_Coverage_Count(test_collector_set, df_item_val, args.need_transform),
        Evaluator_User_Experience(test_collector_set, df_item_val, item_similarity, item_popularity,
                                  args.need_transform, lbe_item=env.lbe_item if args.need_transform else None),
        LoggerEval_Policy(args.force_length, metrics)]
    model_save_path = os.path.join(MODEL_SAVE_PATH, "{}_{}.pt".format(args.model_name, args.message))

    # trainer
    result = onpolicy_trainer(
        policy,
        train_collector,
        test_collector_set,
        args.epoch,
        args.step_per_epoch,
        args.repeat_per_collect,
        args.test_num,
        args.batch_size,
        episode_per_collect=args.episode_per_collect,
        # stop_fn=stop_fn,
        # save_best_fn=save_best_fn,
        # logger=logger1,
        save_model_fn=functools.partial(save_model_fn,
                                        model_save_path=model_save_path,
                                        state_tracker=state_tracker,
                                        optim=optim,
                                        is_save=args.is_save)
    )

    print(__file__)
    pprint.pprint(result)
    logger.info(result)


def main(args):
    # %% 1. Prepare the saved path.
    MODEL_SAVE_PATH, logger_path = prepare_dir_log(args)

    # %% 2. Prepare user model and environment
    ensemble_models = prepare_user_model(args)
    env, train_envs = prepare_train_envs(args, ensemble_models)
    test_envs_dict = prepare_test_envs(args)

    # %% 3. Setup policy
    state_tracker = setup_state_tracker(args, ensemble_models, env, train_envs, test_envs_dict)
    policy, train_collector, test_collector_set, optim = setup_policy_model(args, state_tracker, train_envs, test_envs_dict)

    # %% 4. Learn policy
    learn_policy(args, env, policy, train_collector, test_collector_set, state_tracker, optim, MODEL_SAVE_PATH,
                 logger_path)


if __name__ == '__main__':
    args_all = get_args_all()
    args = get_common_args(args_all)
    args_DORL = get_args_DORL()
    args_all.__dict__.update(args.__dict__)
    args_all.__dict__.update(args_DORL.__dict__)
    try:
        main(args_all)
    except Exception as e:
        var = traceback.format_exc()
        print(var)
        logzero.logger.error(var)