"""Retry the observed interrupted Apple CloudKit request; require validation success."""
import subprocess
import sys
import time


def stapler(operation, target):
    for attempt in range(3):
        result = subprocess.run(['xcrun', 'stapler', operation, str(target)],
                                capture_output=True, text=True)
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        if result.returncode == 0:
            break
        network_interrupted = (result.returncode == 68 and
                               'NSURLErrorDomain' in result.stdout + result.stderr)
        if not network_interrupted or attempt == 2:
            result.check_returncode()
        time.sleep(5 * (attempt + 1))


if __name__ == '__main__':
    stapler(sys.argv[1], sys.argv[2])
