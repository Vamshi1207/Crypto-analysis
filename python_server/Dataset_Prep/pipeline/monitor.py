import psutil
import os


def get_memory():
    return psutil.Process(os.getpid()).memory_info().rss / (1024 ** 3)