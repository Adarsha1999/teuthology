import requests
import sys
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

    builds = []
    for platform in platforms:
        build = fetch_upstream_build(branch=branch, platform=platform, arch=arch)
        build["platform"] = platform   # annotate for error reporting
        builds.append(build)

    # Extract IDs
    shaman_ids = [b["shaman_id"] for b in builds]

    # Check ID consistency across all platforms
    if len(set(shaman_ids)) > 1:
        print("ERROR: Multiple platforms returned *different* shaman_ids:", file=sys.stderr)
        for b in builds:
            print(f"  {b['platform']}: {b['shaman_id']}", file=sys.stderr)
        sys.exit(1)

    write_output(shaman_ids[0], output)