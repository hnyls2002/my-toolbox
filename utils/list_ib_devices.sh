#!/bin/bash
for dev in $(ls /sys/class/infiniband); do
  state=$(cat /sys/class/infiniband/$dev/ports/1/state 2>/dev/null)
  if [[ "$state" == "4: ACTIVE" ]]; then
    echo "$dev is ACTIVE"
  else
    echo "$dev is NOT active (state=$state)"
  fi
done
