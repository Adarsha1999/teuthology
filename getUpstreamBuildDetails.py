from typing import Any
import sys
import requests
from docopt import docopt

BUILD_URL = "https://shaman.ceph.com/api/repos/ceph/{}/latest/{}/{}/flavors/{}"
IMAGE_URL = "quay.ceph.io/ceph-ci/ceph:{}"

doc = """
This script fetches the upstream build details

Usage:
    getUpstreamBuildDetails.py (--branch <branch_name>)
        (--platform <platform>)
        (--arch <arch>)
        [--output <output-path>]

    getTestSuites.py --help

Options:
    -h --help               Shows the command usage
    -b --branch <str>       Ceph upstream branch name
    -p --platform <str>     OS Platform (centos-9)
    -a --arch <str>         OS arch (x86_64)
    -o --output <str>       Output file path
"""

def fetch_upstream_build(branch, platform="centos-9", arch="x86_64"):
    """Method to get build details based on branch

    Args:
        branch (str): Upstream branch name
        platform (str): Operating System (default: centos-9)
        arch (str): CPU Architecture (default: x86_64)
    """
    # Set build variable
    build = {}

    # Get shaman build url
    os_type, os_version, flavors = platform.split("-", 2)
    url = BUILD_URL.format(branch, os_type, os_version, flavors)

    # Disable insecure request warning in response
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

    # Get status with url
    response = requests.get(url, verify=False, timeout=30)
    if not response.ok:
        response.raise_for_status()

    # Get build details from response
    _repo, _id, _version = None, None, None
    for srcs in response.json():
        if arch in srcs["archs"]:
            _version = srcs["extra"]["version"]
            _url = srcs["chacra_url"]
            _repo = f"{_url}repo" if _url.endswith("/") else f"{_url}/repo"
            _id = srcs["sha1"]

            break
    else:
        raise Exception(
            f"Could not find build source for {branch}-{platform}-{arch}"
        )

    # Set build details
    build["repo"] = _repo
    build["version"] = _version

    # Get image for rpm repo
    image = IMAGE_URL.format(_id)
    build["image"] = image
    build["shaman_id"] = _id
    return build

def fetch_all_builds_for_platform(branch, platform, arch="x86_64"):
    """Fetch all available builds for a platform, returning list of (sha1, timestamp) tuples
    
    Args:
        branch (str): Upstream branch name
        platform (str): Operating System (e.g., centos-9-default)
        arch (str): CPU Architecture (default: x86_64)
    
    Returns:
        list: List of dicts with sha1 and timestamp, sorted by timestamp (newest first)
    """
    os_type, os_version, flavors = platform.split("-", 2)
    url = BUILD_URL.format(branch, os_type, os_version, flavors)
    
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )
    
    response = requests.get(url, verify=False, timeout=30)
    if not response.ok:
        response.raise_for_status()
    
    builds = []
    for srcs in response.json():
        if arch in srcs["archs"]:
            sha1 = srcs["sha1"]
            # Try to get timestamp from extra or use modification time
            timestamp = srcs.get("extra", {}).get("timestamp") or srcs.get("modified", "")
            builds.append({
                "sha1": sha1,
                "timestamp": timestamp,
                "version": srcs["extra"].get("version", ""),
                "platform": platform
            })
    
    # Sort by timestamp descending (newest first), fallback to version if no timestamp
    builds.sort(key=lambda x: (x["timestamp"] or "", x["version"]), reverse=True)
    return builds

def check_sha_exists_in_platform(branch, platform, sha1, arch="x86_64"):
    """Check if a specific SHA exists in a platform
    
    Args:
        branch (str): Upstream branch name
        platform (str): Operating System (e.g., centos-9-default)
        sha1 (str): SHA1 to check
        arch (str): CPU Architecture (default: x86_64)
    
    Returns:
        bool: True if SHA exists in platform, False otherwise
    """
    os_type, os_version, flavors = platform.split("-", 2)
    url = BUILD_URL.format(branch, os_type, os_version, flavors)
    
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )
    
    try:
        response = requests.get(url, verify=False, timeout=30)
        if not response.ok:
            return False
        
        for srcs in response.json():
            if arch in srcs["archs"] and srcs["sha1"] == sha1:
                return True
        return False
    except:
        return False

def find_latest_common_build(branch, platforms, arch="x86_64"):
    """Find the latest SHA that exists in ALL specified platforms
    
    Strategy: Get builds from first platform, then check if each SHA exists in other platforms.
    Since /latest endpoint may only return one build, we'll check if that SHA exists in others.
    If not, we'll try to query by SHA or use a different approach.
    
    Args:
        branch (str): Upstream branch name
        platforms (list): List of platform strings (e.g., ["centos-9-default", "ubuntu-jammy-default"])
        arch (str): CPU Architecture (default: x86_64)
    
    Returns:
        str: The latest shaman_id (sha1) that exists in all platforms, or None if not found
    """
    if len(platforms) < 2:
        # Single platform: just get the latest
        builds = fetch_all_builds_for_platform(branch, platforms[0], arch)
        return builds[0]["sha1"] if builds else None
    
    # Get builds from first platform (sorted by newest first)
    primary_platform = platforms[0]
    other_platforms = platforms[1:]
    
    try:
        primary_builds = fetch_all_builds_for_platform(branch, primary_platform, arch)
    except Exception as e:
        print(f"ERROR: Failed to fetch builds for {primary_platform}: {e}", file=sys.stderr)
        return None
    
    if not primary_builds:
        print(f"ERROR: No builds found for {primary_platform}", file=sys.stderr)
        return None
    
    # Check each SHA from primary platform (starting with newest) to see if it exists in all other platforms
    for build in primary_builds:
        sha1 = build["sha1"]
        exists_in_all = True
        
        for other_platform in other_platforms:
            if not check_sha_exists_in_platform(branch, other_platform, sha1, arch):
                exists_in_all = False
                break
        
        if exists_in_all:
            return sha1
    
    # If primary platform's latest doesn't exist in others, try the other way around:
    # Check if other platforms' latest SHAs exist in primary platform
    for other_platform in other_platforms:
        try:
            other_builds = fetch_all_builds_for_platform(branch, other_platform, arch)
            for build in other_builds:
                sha1 = build["sha1"]
                # Check if this SHA exists in primary platform and all other platforms
                if check_sha_exists_in_platform(branch, primary_platform, sha1, arch):
                    # Verify it exists in all other platforms too
                    exists_in_all = True
                    for check_platform in other_platforms:
                        if check_platform != other_platform:
                            if not check_sha_exists_in_platform(branch, check_platform, sha1, arch):
                                exists_in_all = False
                                break
                    if exists_in_all:
                        return sha1
        except Exception as e:
            print(f"WARNING: Failed to check {other_platform}: {e}", file=sys.stderr)
            continue
    
    # No common SHA found
    print(f"ERROR: No common SHA found across platforms: {platforms}", file=sys.stderr)
    print(f"  Checked {len(primary_builds)} builds from {primary_platform}", file=sys.stderr)
    return None

def write_output(data, output):
    """Write output"""
    # Print if output is not provided
    if not output:
        print(data)
        return

    # Write output to file
    with open(output, "w") as fp:
        fp.write(str(data))



if __name__ == "__main__":
    args = docopt(doc)
    branch = args["--branch"].lower()
    arch = args["--arch"].lower()
    output = args.get("--output")
    # Support comma-separated platform list
    platforms = [p.strip().lower() for p in args["--platform"].split(",")]

    # If multiple platforms specified, find latest common SHA
    if len(platforms) > 1:
        latest_common_sha = find_latest_common_build(branch, platforms, arch)
        if not latest_common_sha:
            print(f"ERROR: Could not find a common SHA across platforms: {platforms}", file=sys.stderr)
            sys.exit(1)
        write_output(latest_common_sha, output)
    else:
        # Single platform: use existing logic
        build = fetch_upstream_build(branch=branch, platform=platforms[0], arch=arch)
        write_output(build["shaman_id"], output)
