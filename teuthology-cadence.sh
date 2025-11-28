#!/usr/bin/env bash
set -euo pipefail

tmp_err=$(mktemp)
trap 'rm -f "$tmp_err"' EXIT

if ! shaman_id=$(python3 getUpstreamBuildDetails.py \
  --branch tentacle \
  --platform ubuntu-jammy-default,centos-9-default,centos-9-crimson-debug \
  --arch x86_64 2>"$tmp_err"); then
  echo "ERROR: Failed to get upstream build details:" >&2
  cat "$tmp_err" >&2
  exit 1
fi

shaman_id=$(python3 getUpstreamBuildDetails.py \
  --branch tentacle \
  --platform ubuntu-jammy \
  --arch x86_64)

echo "Using shaman build id: $shaman_id"

run_suite() {
  suite=$1
  extra_args=$2

  # Generate a single random number between 1–10
  rand=$(( (RANDOM % 10) + 1 ))

  echo "[$(date)] Running suite: $suite with limit=$rand job-threshold=$rand subset=${rand}/10000"

  teuthology-suite \
    --suite "$suite" \
    --machine-type openstack \
    --ceph tentacle \
    --ceph-repo https://github.com/ceph/ceph \
    --limit $rand \
    --job-threshold $rand \
    --subset ${rand}/10000 \
    --sha1 $shaman_id \
    $extra_args
}

run_suite "teuthology:nop" ""
sleep 1h

run_suite "smoke" "/home/ubuntu/override.yaml"
sleep 2h

run_suite "orch" ""
sleep 3h

run_suite "krbd" "/home/ubuntu/override.yaml"
sleep 3h

run_suite "crimson-rados/basic" "--filter objectstore/bluestore --flavor crimson-debug /home/ubuntu/crimson-override.yaml"
sleep 3h

run_suite "rgw" "/home/ubuntu/override.yaml"
sleep 3h

run_suite "fs" "/home/ubuntu/override.yaml"
sleep 3h

run_suite "upgrade" "/home/ubuntu/override.yaml"
sleep 3h

run_suite "rados" "/home/ubuntu/override.yaml"
sleep 3h
