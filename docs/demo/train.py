"""Stand-in training script for the autosentry demo.

Emits realistic training log lines. At BATCH_SIZE >= 8 it runs out of
memory partway through and dies with a CUDA OOM traceback; at anything
smaller it trains to completion. That gives the demo a failure the
`oom_halve_batch` rule can actually fix, rather than a scripted fake.
"""

import os
import sys
import time

BATCH = int(os.environ.get("BATCH_SIZE", "8"))
STEP_DELAY = float(os.environ.get("DEMO_STEP_DELAY", "0.28"))


def log(msg: str) -> None:
    print(msg, flush=True)


log(f"loading dataset shards … ok  (batch_size={BATCH})")
time.sleep(STEP_DELAY)

for step in range(1, 6):
    if BATCH >= 8 and step == 4:
        sys.stderr.write(
            "Traceback (most recent call last):\n"
            '  File "train.py", line 61, in <module>\n'
            "    loss = model(batch).backward()\n"
            "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to "
            "allocate 2.31 GiB (GPU 0; 23.68 GiB total capacity)\n"
        )
        sys.stderr.flush()
        raise SystemExit(1)
    log(f"step {step}/5  loss {2.91 - step * 0.17:.3f}  {14 * BATCH} tok/s")
    time.sleep(STEP_DELAY)

log("training complete — checkpoint written to out/final.pt")
