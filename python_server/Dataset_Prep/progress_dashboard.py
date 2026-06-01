import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Dataset_Prep.progress import DashboardServer


def main():
    run_dir = Path(sys.argv[1])
    host = sys.argv[2]
    port = int(sys.argv[3])

    server = DashboardServer(run_dir, host, port)
    server.start()
    server.thread.join()


if __name__ == "__main__":
    main()
