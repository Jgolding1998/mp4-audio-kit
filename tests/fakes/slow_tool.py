"""Stand-in for ffmpeg that never finishes, for timeout and disconnect tests."""
import time

time.sleep(120)
