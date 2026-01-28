#!/usr/bin/env bash
set -euo pipefail

# Script to run all teuthology suites sequentially with teuthology-wait
# Continuously alternates between main and tentacle branches
# Based on upstream-crontab configuration
# Usage: run-all-suites.sh [override_yaml]
# To stop: Create /home/ubuntu/teuthology/stop-all-suites.flag or send SIGTERM/SIGINT

SCRIPT_DIR="/home/ubuntu/teuthology"
OVERRIDE_YAML="${1:-/home/ubuntu/override.yaml}"
LOG_DIR="$HOME/cadence_logs"
LOG_FILE="$LOG_DIR/all-suites-$(date +%Y%m%d-%H%M%S).log"
STOP_FLAG="$SCRIPT_DIR/stop-all-suites.flag"
CRON_LOG_DIR="$HOME/cadence_logs"

# Trap signals to allow graceful shutdown
trap 'log "Received signal, stopping after current branch completes..."; touch "$STOP_FLAG"; exit 0' SIGTERM SIGINT

# Create logs directory if it doesn't exist
mkdir -p "$LOG_DIR"
mkdir -p "$CRON_LOG_DIR"

# Change to script directory
cd "$SCRIPT_DIR"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Function to extract flavor from extra_args
extract_flavor() {
  local extra_args="$1"
  if echo "$extra_args" | grep -q -- '--flavor'; then
    echo "$extra_args" | grep -o -- '--flavor[[:space:]]\+[^[:space:]]\+' | awk '{print $2}'
  else
    echo "default"
  fi
}

# Function to construct run name
construct_run_name() {
  local suite="$1"
  local timestamp="$2"
  local ceph_branch="$3"
  local flavor="$4"
  local user=$(whoami)
  local kernel_branch="distro"
  local worker="openstack"
  
  suite=$(echo "$suite" | sed 's/\//:/g')
  echo "${user}-${timestamp}-${suite}-${ceph_branch}-${kernel_branch}-${flavor}-${worker}"
}

# Function to get suite partitions (from upstream-crontab logic)
get_suite_partitions() {
  local suite="$1"
  case "$suite" in
    "teuthology:nop")
      echo 1
      ;;
    "orch")
      echo 64
      ;;
    "krbd")
      echo 4
      ;;
    "crimson-rados/basic"|"crimson-rados")
      echo 1
      ;;
    "rgw")
      echo 30000
      ;;
    "fs")
      echo 512
      ;;
    "rados")
      echo 100000
      ;;
    "rbd")
      echo 128
      ;;
    "upgrade")
      echo 32
      ;;
    *)
      echo 20
      ;;
  esac
}

# Function to run a suite for a branch and wait for completion
run_suite_and_wait() {
  local branch="$1"
  local suite="$2"
  local extra_args="$3"
  local priority="${4:-950}"
  local suite_override_yaml="${5:-}"  # Optional suite-specific override yaml
  local tmp_err=$(mktemp)
  
  log "Starting suite: $suite for branch: $branch"
  
  # Get shaman_id for the branch
  # For crimson-debug flavor, use only centos-9-crimson-debug platform
  # For regular suites, use ubuntu-jammy-default,centos-9-default (may have different shaman_ids)
  local flavor=$(extract_flavor "$extra_args")
  local platform_list
  if [ "$flavor" = "crimson-debug" ]; then
    platform_list="centos-9-crimson-debug"
  else
    # For regular suites, try to get common build from both platforms
    # If they differ, we'll use ubuntu-jammy-default as primary
    platform_list="ubuntu-jammy-default,centos-9-default"
  fi
  
  # Check if shaman_id is provided via environment variable (for testing/override)
  # OVERRIDE_SHAMAN_ID is branch-specific: use it only for tentacle branch
  # For main branch, always fetch from Shaman
  if [ -n "${OVERRIDE_SHAMAN_ID:-}" ] && [ "$branch" = "tentacle" ]; then
    shaman_id="$OVERRIDE_SHAMAN_ID"
    log "Using override shaman_id from environment for tentacle branch: $shaman_id"
  else
    # Try to get shaman_id from all platforms (they may differ)
    if ! shaman_id=$(python3 getUpstreamBuildDetails.py \
      --branch "$branch" \
      --platform "$platform_list" \
      --arch x86_64 2>"$tmp_err"); then
      # If platforms have different shaman_ids, use ubuntu-jammy-default as fallback
      log "WARNING: Platforms returned different shaman_ids, using ubuntu-jammy-default as primary"
      if ! shaman_id=$(python3 getUpstreamBuildDetails.py \
        --branch "$branch" \
        --platform "ubuntu-jammy-default" \
        --arch x86_64 2>"$tmp_err"); then
        log "ERROR: Failed to get upstream build details for branch $branch:"
        cat "$tmp_err" | tee -a "$LOG_FILE"
        rm -f "$tmp_err"
        return 1
      fi
    fi
  fi
  
  log "Using shaman build id for branch $branch: $shaman_id"
  rm -f "$tmp_err"
  
  # Upload shaman_id to remote server
  sshpass -p "admin" ssh -o StrictHostKeyChecking=no cloud-user@10.0.196.233 \
    "sudo mkdir -p /data/scheduler/cron && echo '${shaman_id}' | sudo tee /data/scheduler/cron/${branch}-$(date "+%Y-%m-%d") > /dev/null" 2>&1 | tee -a "$LOG_FILE"
  
  # Unlock targets before running
  # Use unlock_safe which checks for active jobs before unlocking
  # This prevents deleting nodes/volumes that are currently in use
  log "Unlocking targets (only stale/unused nodes will be unlocked, active jobs are protected)..."
  if ! teuthology-lock --list-targets --owner scheduled_ubuntu@teuth-teuthology > ~/locked_targets 2>> "$LOG_FILE"; then
    log "WARNING: Failed to list targets, continuing anyway..."
  fi
  # Only unlock if we have targets listed
  if [ -s ~/locked_targets ]; then
    # Extract node names from targets file and use unlock_safe via Python
    # This ensures nodes with active jobs are NOT deleted
    if python3 <<EOF 2>> "$LOG_FILE"
import sys
import yaml
from teuthology.lock import ops, query

owner = "scheduled_ubuntu@teuth-teuthology"
targets_file = "$HOME/locked_targets"

try:
    with open(targets_file) as f:
        targets_data = yaml.safe_load(f)
        if not targets_data or 'targets' not in targets_data:
            sys.exit(0)
        node_names = list(targets_data['targets'].keys())
        if not node_names:
            sys.exit(0)
        
        # Use unlock_safe which checks for active jobs
        unlocked = ops.unlock_safe(node_names, owner)
        if unlocked:
            print(f"Successfully unlocked {len(node_names)} nodes")
        else:
            print(f"Some nodes could not be unlocked (may have active jobs)")
except Exception as e:
    print(f"Error unlocking targets: {e}", file=sys.stderr)
    sys.exit(1)
EOF
    then
      log "Targets unlocked safely (active jobs were protected)"
    else
      log "WARNING: Failed to unlock some targets (they may have active jobs), continuing anyway..."
    fi
  else
    log "No targets to unlock"
  fi
  
  # Get suite partitions and calculate subset
  # Match upstream schedule_subset.sh behavior: use RANDOM for subset selection
  # This ensures consistent job counts (jobs are divided evenly by partitions)
  # while providing variety in which subset runs each time
  local partitions=$(get_suite_partitions "$suite")
  
  # Match upstream schedule_subset.sh exactly:
  # ARGS+=("--subset=$((RANDOM % partitions))/$partitions")
  # Note: upstream uses --subset=X/Y format (with equals sign)
  local subset_arg=""
  if [ "$suite" = "rgw" ]; then
    # rgw: matches upstream-crontab line 71 (explicit subset, but we calculate like schedule_subset.sh)
    local random_subset=$((RANDOM % 30000))
    subset_arg="--subset=${random_subset}/30000"
  else
    # Use RANDOM % partitions (matches schedule_subset.sh line 14 exactly)
    local random_subset=$((RANDOM % partitions))
    subset_arg="--subset=${random_subset}/${partitions}"
  fi
  
  # Note: We do NOT set --seed (matches upstream schedule_subset.sh behavior)
  # teuthology-suite will use default seed=-1, which generates a random seed
  # This matches upstream behavior where seed is not set
  
  log "Starting suite $suite for branch $branch with subset=${subset_arg} priority=$priority"
  
  # Capture timestamp
  local timestamp=$(date "+%Y-%m-%d_%H:%M:%S")
  
  # Build command
  local cmd="teuthology-suite \
      --suite \"$suite\" \
      --machine-type openstack \
      --ceph \"$branch\" \
      --ceph-repo https://github.com/ceph/ceph \
      --priority $priority \
      --force-priority \
      $subset_arg \
      --sha1 $shaman_id"
  
  # Use suite-specific override yaml if provided, otherwise use default
  local override_file="${suite_override_yaml:-$OVERRIDE_YAML}"
  cmd="$cmd $extra_args $override_file"
  
  log "Running command: $cmd"
  
  # Execute teuthology-suite and capture output
  local suite_output=$(mktemp)
  if ! eval "$cmd" > "$suite_output" 2>&1; then
    log "ERROR: Failed to schedule suite $suite for branch $branch"
    cat "$suite_output" >> "$LOG_FILE"
    rm -f "$suite_output"
    return 1
  fi
  
  # Append output to log
  cat "$suite_output" >> "$LOG_FILE"
  
  # Extract run name from teuthology-suite output
  local run_name
  run_name=$(grep -oP "Job scheduled with name \K[^\s]+" "$suite_output" | head -1)
  rm -f "$suite_output"
  
  if [ -z "$run_name" ]; then
    log "WARNING: Could not extract run name, constructing it..."
    run_name=$(construct_run_name "$suite" "$timestamp" "$branch" "$flavor")
  fi
  
  log "Using run name: $run_name"
  
  # Wait for run to be registered
  log "Waiting 10 seconds for run to be registered on server..."
  sleep 10
  
  # Wait for suite to complete using teuthology-wait
  log "Waiting for suite $suite (branch: $branch, run: $run_name) to complete using teuthology-wait..."
  if ! teuthology-wait --run "$run_name" >> "$LOG_FILE" 2>&1; then
    log "WARNING: Suite $suite for branch $branch completed with failures or errors"
    return 1
  else
    log "✓ Suite $suite for branch $branch completed successfully"
    return 0
  fi
}

# Function to run all suites for a branch
run_all_suites_for_branch() {
  local branch="$1"
  
  log "=========================================="
  log "Starting all suites for branch: $branch"
  log "=========================================="
  log ""
  
  # Run all suites for the branch
  if [ "$branch" = "main" ]; then
    # teuthology/nop on main branch
    log "=== Suite: teuthology/nop (main) ==="
    if run_suite_and_wait "main" "teuthology:nop" "" "1"; then
        log "✓ teuthology/nop completed"
    else
        log "✗ teuthology/nop had errors"
    fi
    log ""
    
    # main branch suites
    log "=== Main branch suites ==="
    # Do not stop the orchestrator on failures: each suite is best-effort.
    # NOTE: This script runs with `set -e`, so we must guard non-zero returns.
    #run_suite_and_wait "main" "orch" "--filter-out nvme" "950" || log "✗ Suite orch (main) had errors"
    #run_suite_and_wait "main" "rbd" "" "950" || log "✗ Suite rbd (main) had errors"
    #run_suite_and_wait "main" "fs" "" "700" || log "✗ Suite fs (main) had errors"
    #run_suite_and_wait "main" "rgw" "" "150" || log "✗ Suite rgw (main) had errors"
    run_suite_and_wait "main" "krbd" "--kernel testing" "950" || log "✗ Suite krbd (main) had errors"
    run_suite_and_wait "main" "crimson-rados/basic" "--filter objectstore/bluestore" "101" "/home/ubuntu/crimson-override.yaml" || log "✗ Suite crimson-rados/basic (main) had errors"
    log ""
    
    # main upgrade and rados at the end
    log "=== Main upgrade and rados (at end) ==="
    run_suite_and_wait "main" "upgrade" "" "850" || log "✗ Suite upgrade (main) had errors"
    run_suite_and_wait "main" "rados" "" "101" || log "✗ Suite rados (main) had errors"
    log ""
  else
    # tentacle branch suites
    log "=== Tentacle branch suites ==="
    run_suite_and_wait "tentacle" "orch" "--filter-out nvme" "830" || log "✗ Suite orch (tentacle) had errors"
    run_suite_and_wait "tentacle" "rbd" "" "830" || log "✗ Suite rbd (tentacle) had errors"
    run_suite_and_wait "tentacle" "fs" "" "830" || log "✗ Suite fs (tentacle) had errors"
    run_suite_and_wait "tentacle" "rgw" "" "830" || log "✗ Suite rgw (tentacle) had errors"
    run_suite_and_wait "tentacle" "krbd" "--kernel testing" "830" || log "✗ Suite krbd (tentacle) had errors"
    run_suite_and_wait "tentacle" "crimson-rados/basic" "--flavor crimson-debug --filter objectstore/bluestore" "830" "/home/ubuntu/crimson-override.yaml" || log "✗ Suite crimson-rados/basic (crimson-debug, tentacle) had errors"
    log ""
    
    # tentacle upgrade and rados at the end
    log "=== Tentacle upgrade and rados (at end) ==="
    run_suite_and_wait "tentacle" "upgrade" "" "150" || log "✗ Suite upgrade (tentacle) had errors"
    run_suite_and_wait "tentacle" "rados" "" "831" || log "✗ Suite rados (tentacle) had errors"
    log ""
  fi
  
  log "=========================================="
  log "All suites for branch $branch completed"
  log "=========================================="
  log ""
}

# Function to check if we should stop
should_stop() {
  [ -f "$STOP_FLAG" ]
}

# Main execution - continuous loop alternating between branches
log "=========================================="
log "Starting continuous all suites execution"
log "Log file: $LOG_FILE"
log "To stop: touch $STOP_FLAG or send SIGTERM/SIGINT"
log "=========================================="
log ""

# Remove stop flag if it exists from previous run
rm -f "$STOP_FLAG"

# Start with main branch
current_branch="main"
iteration=1

while true; do
  # Check if we should stop before starting a new branch cycle
  if should_stop; then
    log "Stop flag detected, exiting gracefully..."
    rm -f "$STOP_FLAG"
    break
  fi
  
  log "=========================================="
  log "Iteration $iteration: Running suites for $current_branch branch"
  log "=========================================="
  log ""
  
  # Run all suites for current branch
  run_all_suites_for_branch "$current_branch"
  
  # Check if we should stop after completing a branch
  if should_stop; then
    log "Stop flag detected, exiting gracefully..."
    rm -f "$STOP_FLAG"
    break
  fi
  
  # Switch to the other branch for next iteration
  if [ "$current_branch" = "main" ]; then
    current_branch="tentacle"
  else
    current_branch="main"
  fi
  
  iteration=$((iteration + 1))
  
  log "=========================================="
  log "Switching to $current_branch branch for next iteration"
  log "=========================================="
  log ""
done

log "=========================================="
log "Continuous all suites execution stopped"
log "=========================================="


