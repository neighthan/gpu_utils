from subprocess import run, PIPE
import re
import os
import sys
from collections import namedtuple
from time import time, sleep
import signal
from argparse import ArgumentParser, RawDescriptionHelpFormatter

def lock_exists() -> bool:
    locks = list(filter(lambda fname: fname.startswith('.job_lock'), os.listdir(lock_dir)))
    return len(locks) > 0

def make_lock() -> None:
    open(f"{lock_dir}/.job_lock_{lock_suffix}", 'w').close()

def remove_lock() -> None:
    try:
        os.remove(f"{lock_dir}/.job_lock_{lock_suffix}")
    except FileNotFoundError:
        pass

def check_lock() -> None:
    locks = list(filter(lambda fname: fname.startswith('.job_lock'), os.listdir(lock_dir)))
    assert len(locks) == 1, "Multiple locks found! {}".format('\n'.join(locks))
    assert locks[0] == f'.job_lock_{lock_suffix}', f'Found lock {locks[0]} when expecting .job_lock_{lock_suffix}.'

def cleanup(*args) -> None:
    """
    :param args: unused; given by signal
    """

    remove_lock()
    sys.exit(0)


if __name__ == '__main__':
    parser = ArgumentParser(description="expected format of the job file is (pipe-delimited):\n"
                                        "mem_free_threshold|util_free_threshold|command_to_run\n"
                                        "command_to_run should have {} where the device number should be inserted. Ex:\n"
                                        "4000|30|python my_script.py -flag --other=7 --device={}\n"
                                        "This will execute the command above once there's a gpu with 4 GB free and that "
                                        "has at least 30% utilization free. {} will be replaced with the number of the "
                                        "gpu which the job should use' &' will be added to the commands so that they run "
                                        "in the background; this shouldn't already be part of the command",
                            formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument('-f', '--job_file', help="Path to the file listing the jobs to run. The lock will be made in the "
                                                 "same directory as the jobs file.", required=True)
    parser.add_argument('-n', '--n_passes', help="The number of times to check nvidia-smi. Memory used by each gpu will "
                                                 "be the max value of all checks and utilization used will be the mean. "
                                                 "[default = 4]", type=int, default=4)
    parser.add_argument('-st', '--sleep_time', help="How long, in seconds, to sleep between trying to start jobs when either "
                                                   "the gpus are full, no jobs exist, or we're waiting to acquire the lock. "
                                                   "[default = 60]", type=float, default=60)
    parser.add_argument('-kt', '--keep_time', help="How long, in seconds, to wait for a process to show up on the GPUs "
                                                   "before assuming that it crashed (and letting the resources reserved "
                                                   "for it be used by other processes). [default = 120]", type=float, default=120)
    parser.add_argument('-ls', '--lock_suffix', help="Suffix that this script will use to tell that the lock on the file "
                                                     "belongs to it. This shouldn't be used by any other script. "
                                                     "[default = runner]", default='runner')
    parser.add_argument('-v', '--verbose', help='How much information to print while running. 0 for none, 1 for some, '
                                                '2 for even more. [default = 0]', type=int, default=0)

    args = parser.parse_args()
    job_file = args.job_file
    verbose = args.verbose
    n_passes = args.n_passes
    sleep_time = args.sleep_time
    keep_time = args.keep_time
    lock_suffix = args.lock_suffix
    lock_dir = os.path.dirname(job_file)

    GPU = namedtuple('GPU', ['num', 'mem_free', 'util_free'])  # mem in MiB, util as % not used

    NewProcess = namedtuple('NewProcess', ['command', 'gpu_num', 'mem_needed', 'util_needed', 'timestamp'])
    new_processes = []

    mem_pattern = re.compile('(\d+)MiB / (\d+)MiB')
    util_pattern = re.compile('\d+MiB\s+\|\s+(\d+)%')  # find the percent after the memory; the one before is about cooling percent

    # try to prevent this process from exiting without releasing the lock
    for sig in [signal.SIGTERM]:
        signal.signal(sig, cleanup)

    try:

        while True:
            assert not os.path.isfile(f"{lock_dir}/.job_lock_{lock_suffix}"), "runner lock wasn't released!"

            ### check if you have any jobs
            while not os.path.isfile(job_file):
                sleep(sleep_time)

            while lock_exists():
                sleep(sleep_time)

            make_lock()
            try:
                check_lock() # fails if we aren't the sole lock-holder now
            except AssertionError:
                remove_lock()
                continue

            with open(job_file) as f:
                job_spec = f.readline().strip()

                if not job_spec: # empty file
                    remove_lock()
                    sleep(sleep_time)
                    continue

                mem_needed, util_needed, job_script = job_spec.split('|')
                mem_needed = int(mem_needed)
                util_needed = int(util_needed)

                other_jobs = f.readlines()

                if verbose:
                    print('Found job (+ {} others)\n\t{}'.format(len(other_jobs), job_spec))

            ### check if there's a gpu you can run this job on (enough memory and util free)
            gpus = {}

            for _ in range(n_passes):
                info_string = run('nvidia-smi', stdout=PIPE).stdout.decode()

                mem_usage = list(re.finditer(mem_pattern, info_string))
                util_usage = list(re.finditer(util_pattern, info_string))

                for i in range(len(mem_usage)):
                    gpu = GPU(i, int(mem_usage[i].group(2)) - int(mem_usage[i].group(1)), 100 - int(util_usage[i].group(1)))
                    try:
                        gpus[i].append(gpu)
                    except KeyError:
                        gpus[i] = [gpu]

            # remove processes that have shown up on the GPU
            new_processes = list(filter(lambda process: process.command not in info_string, new_processes))
            # if a process doesn't show up on the GPU after enough time, assume it had an error and crashed; remove
            now = time()
            new_processes = list(filter(lambda process: now - process.timestamp < keep_time, new_processes))

            if verbose > 1:
                print('New processes not yet seen on GPU:')
                print('\n'.join(str(process) for process in new_processes))

            # subtract mem and util used by new processes from that which is shown to be free
            mem_newly_used = [0] * len(gpus)
            util_newly_used = [0] * len(gpus)
            for process in new_processes:
                mem_newly_used[process.gpu_num] += process.mem_needed
                util_newly_used[process.gpu_num] += process.util_needed

            # set mem_free to max from each pass, util_free to mean
            gpus = [GPU(i,
                        max([gpu.mem_free for gpu in gpu_list]) - mem_newly_used[i],
                        sum([gpu.util_free for gpu in gpu_list]) / len(gpu_list) - util_newly_used[i])
                    for i, gpu_list in enumerate(gpus.values())]

            gpus = filter(lambda gpu: gpu.mem_free >= mem_needed and gpu.util_free >= util_needed, gpus)

            try:
                best_gpu = max(gpus, key=lambda gpu: gpu.util_free)

                if verbose:
                    print(f"Selected best gpu: {best_gpu}")
            except ValueError: # max gets no gpus because none have enough mem_free and util_free
                remove_lock()
                sleep(sleep_time)
                continue

            job_script = job_script.format(best_gpu.num)
            run(f'{job_script} &', shell=True) # make sure to background the script

            if verbose:
                print("Started job:\n\t{}".format(job_script))

            new_processes.append(NewProcess(job_script, best_gpu.num, mem_needed=mem_needed, util_needed=util_needed, timestamp=time()))

            # this job is running, so remove it from the list
            with open(job_file, 'w') as f:
                f.writelines(other_jobs)

            remove_lock()

    finally:
        remove_lock()
