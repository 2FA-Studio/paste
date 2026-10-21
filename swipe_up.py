import subprocess
import time

try:
    while True:
        subprocess.run(["hdc", "shell", "uitest", "uiInput", "dircFling", "2"], check=True)
        time.sleep(0.3)
except KeyboardInterrupt:
    pass
