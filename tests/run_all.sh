#!/bin/bash
# Run every test that needs no camera, Jetson or network. Exit code is the number of failures.
#   bash tests/run_all.sh
# Not run here (they need the live camera service, see tests/README.md):
#   test_end_to_end.py test_falls.py test_multiperson.py test_reid_safe.py, the labs and calibration scripts.
cd "$(dirname "$0")/.." || exit 1
LIVE="test_end_to_end.py test_falls.py test_multiperson.py test_reid_safe.py"
fail=0; pass=0
for f in tests/test_*.py tests/test_*.sh; do
  b="$(basename "$f")"
  case " $LIVE " in *" $b "*) continue;; esac
  if [ "${f##*.}" = "sh" ]; then run="bash $f"; else run="python3 $f"; fi
  if $run >/tmp/vigil_test_$b.log 2>&1; then pass=$((pass+1)); echo "PASS  $b"
  else fail=$((fail+1)); echo "FAIL  $b   (log: /tmp/vigil_test_$b.log)"; fi
done
echo "$pass passed, $fail failed"
exit $fail
