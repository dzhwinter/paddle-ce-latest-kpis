from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import argparse
import time

import paddle
import paddle.fluid as fluid
import paddle.fluid.profiler as profiler

from continuous_evaluation import (train_acc_kpi, train_cost_kpi, test_acc_kpi,
                                   train_duration_kpi, tracking_kpis)
SEED = 90
DTYPE = "float32"

# random seed must set before configuring the network.
fluid.default_startup_program().random_seed = SEED


def parse_args():
    parser = argparse.ArgumentParser("mnist model benchmark.")
    parser.add_argument(
        '--batch_size', type=int, default=128, help='The minibatch size.')
    parser.add_argument(
        '--iterations',
        type=int,
        default=35,
        help='The number of minibatches.')
    parser.add_argument(
        '--pass_num', type=int, default=5, help='The number of passes.')
    parser.add_argument(
        '--device',
        type=str,
        default='GPU',
        choices=['CPU', 'GPU'],
        help='The device type.')
    parser.add_argument(
        '--infer_only', action='store_true', help='If set, run forward only.')
    parser.add_argument(
        '--use_cprof', action='store_true', help='If set, use cProfile.')
    parser.add_argument(
        '--use_nvprof',
        action='store_true',
        help='If set, use nvprof for CUDA.')
    args = parser.parse_args()
    return args


def print_arguments(args):
    vars(args)['use_nvprof'] = (vars(args)['use_nvprof'] and
                                vars(args)['device'] == 'GPU')
    print('-----------  Configuration Arguments -----------')
    for arg, value in sorted(vars(args).iteritems()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------------')


def cnn_model(data):
    conv_pool_1 = fluid.nets.simple_img_conv_pool(
        input=data,
        filter_size=5,
        num_filters=20,
        pool_size=2,
        pool_stride=2,
        act="relu")
    conv_pool_2 = fluid.nets.simple_img_conv_pool(
        input=conv_pool_1,
        filter_size=5,
        num_filters=50,
        pool_size=2,
        pool_stride=2,
        act="relu")

    # TODO(dzhwinter) : refine the initializer and random seed settting
    SIZE = 10
    input_shape = conv_pool_2.shape
    param_shape = [reduce(lambda a, b: a * b, input_shape[1:], 1)] + [SIZE]
    scale = (2.0 / (param_shape[0]**2 * SIZE))**0.5

    predict = fluid.layers.fc(
        input=conv_pool_2,
        size=SIZE,
        act="softmax",
        param_attr=fluid.param_attr.ParamAttr(
            initializer=fluid.initializer.NormalInitializer(
                loc=0.0, scale=scale)))
    return predict


def eval_test(exe, batch_acc, batch_size_tensor, inference_program):
    test_reader = paddle.batch(
        paddle.dataset.mnist.test(), batch_size=args.batch_size)
    test_pass_acc = fluid.average.WeightedAverage()
    for batch_id, data in enumerate(test_reader()):
        img_data = np.array(map(lambda x: x[0].reshape([1, 28, 28]),
                                data)).astype(DTYPE)
        y_data = np.array(map(lambda x: x[1], data)).astype("int64")
        y_data = y_data.reshape([len(y_data), 1])

        acc, weight = exe.run(inference_program,
                              feed={"pixel": img_data,
                                    "label": y_data},
                              fetch_list=[batch_acc, batch_size_tensor])
        test_pass_acc.add(value=acc, weight=weight)
        pass_acc = test_pass_acc.eval()
    return pass_acc


def run_benchmark(model, args):
    if args.use_cprof:
        pr = cProfile.Profile()
        pr.enable()
    start_time = time.time()
    # Input data
    images = fluid.layers.data(name='pixel', shape=[1, 28, 28], dtype=DTYPE)
    label = fluid.layers.data(name='label', shape=[1], dtype='int64')

    # Train program
    predict = model(images)
    cost = fluid.layers.cross_entropy(input=predict, label=label)
    avg_cost = fluid.layers.mean(x=cost)

    # Evaluator
    batch_size_tensor = fluid.layers.create_tensor(dtype='int64')
    batch_acc = fluid.layers.accuracy(
        input=predict, label=label, total=batch_size_tensor)

    # inference program
    inference_program = fluid.default_main_program().clone()
    with fluid.program_guard(inference_program):
        inference_program = fluid.io.get_inference_program(
            target_vars=[batch_acc, batch_size_tensor])

    # Optimization
    opt = fluid.optimizer.AdamOptimizer(
        learning_rate=0.001, beta1=0.9, beta2=0.999)
    opt.minimize(avg_cost)

    fluid.memory_optimize(fluid.default_main_program())

    # Initialize executor
    place = fluid.CPUPlace() if args.device == 'CPU' else fluid.CUDAPlace(0)
    exe = fluid.Executor(place)

    # Parameter initialization
    exe.run(fluid.default_startup_program())

    # Reader
    train_reader = paddle.batch(
        paddle.dataset.mnist.train(), batch_size=args.batch_size)

    accuracy = fluid.average.WeightedAverage()
    for pass_id in range(args.pass_num):
        accuracy.reset()
        pass_start = time.time()
        every_pass_loss = []
        for batch_id, data in enumerate(train_reader()):
            img_data = np.array(
                map(lambda x: x[0].reshape([1, 28, 28]), data)).astype(DTYPE)
            y_data = np.array(map(lambda x: x[1], data)).astype("int64")
            y_data = y_data.reshape([len(y_data), 1])

            start = time.time()
            loss, acc, weight = exe.run(
                fluid.default_main_program(),
                feed={"pixel": img_data,
                      "label": y_data},
                fetch_list=[avg_cost, batch_acc, batch_size_tensor]
            )  # The accuracy is the accumulation of batches, but not the current batch.
            end = time.time()
            accuracy.add(value=acc, weight=weight)
            every_pass_loss.append(loss)
            print ("Pass = %d, Iter = %d, Loss = %f, Accuracy = %f" %
                    (pass_id, batch_id, loss, acc))

        pass_end = time.time()

        train_avg_acc = accuracy.eval()
        train_avg_loss = np.mean(every_pass_loss)
        test_avg_acc = eval_test(exe, batch_acc, batch_size_tensor,
                                 inference_program)

        print("pass=%d, train_avg_acc=%f,train_avg_loss=%f, test_avg_acc=%f, elapse=%f" %
              (pass_id, train_avg_acc, train_avg_loss, test_avg_acc, (pass_end - pass_start)))

        train_acc_kpi.add_record(np.array(train_avg_acc, dtype='float32'))
        train_cost_kpi.add_record(np.array(train_avg_loss, dtype='float32'))
        test_acc_kpi.add_record(np.array(test_avg_acc, dtype='float32'))
        train_duration_kpi.add_record(pass_end - pass_start)


if __name__ == '__main__':
    args = parse_args()
    print_arguments(args)
    if args.use_nvprof and args.device == 'GPU':
        with profiler.cuda_profiler("cuda_profiler.txt", 'csv') as nvprof:
            run_benchmark(cnn_model, args)
    else:
        run_benchmark(cnn_model, args)
    for kpi in tracking_kpis:
        kpi.persist()
