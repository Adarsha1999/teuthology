"""
Real-time log masking for Teuthology.

Masks sensitive data (IPs, SSH keys, hostnames) in log output.
"""

import logging
import re
from collections import OrderedDict

# Global reference to the active masking filter (used to apply to new handlers)
_active_filter = None


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that masks sensitive information in real-time.
    Hostnames and IPs are mapped to consistent node identifiers (node1, node2, etc.)
    """
    
    # Mask placeholders
    SSH_KEY_MASK = '<MASKED_SSH_KEY>'
    KEY_PATH_MASK = '<MASKED_KEY_PATH>'
    
    # IPv4 pattern - exclude version numbers (e.g., 5.15.0.164.159, ubuntu0.22.04.1)
    # Real IPs should be surrounded by whitespace, punctuation, or start/end of string
    # Not adjacent to letters or part of longer numeric sequences
    IPV4_RE = re.compile(
        r'(?<![a-zA-Z\d\.])'  # Not preceded by letter, digit, or dot
        r'(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}'
        r'(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
        r'(?![a-zA-Z\d\.])'   # Not followed by letter, digit, or dot
    )
    
    # SSH public key pattern (ssh-rsa, ssh-ed25519, ecdsa-sha2-*)
    SSH_KEY_RE = re.compile(
        r'(?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp(?:256|384|521))\s+'
        r'[A-Za-z0-9+/=]{20,}'
    )
    
    # SSH private key block
    SSH_PRIVKEY_RE = re.compile(
        r'-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----',
        re.MULTILINE
    )
    
    # SSH key file paths (id_rsa, id_ed25519, *.pem, cephkey, etc.)
    SSH_KEY_PATH_RE = re.compile(
        r'(?:/[\w.-]+)+/(?:id_(?:rsa|dsa|ecdsa|ed25519)|[\w.-]*(?:key|pem|cephkey)[\w.-]*)',
        re.IGNORECASE
    )
    
    # Hostname patterns - ONLY match actual machine/test node names, NOT public domains
    # Order matters - more specific patterns should come first
    HOST_PATTERNS = [
        # ip-* prefixed hostnames - LONG form first (with domain): ip-10-0-1-2.domain.com
        re.compile(r'\bip-\d+-\d+-\d+-\d+\.[\w.-]+\b', re.IGNORECASE),
        # ip-* prefixed hostnames - SHORT form (no domain): ip-10-0-196-28
        re.compile(r'\bip-\d+-\d+-\d+-\d+\b', re.IGNORECASE),
        re.compile(r'\btarget\d+\.[\w.-]+\b', re.IGNORECASE),  # target001.domain.com
        re.compile(r'\btarget\d+\b', re.IGNORECASE),  # target001 (short form)
        # Ceph lab specific hostnames
        re.compile(r'\b[\w-]+\.front\.sepia\.ceph\.com\b', re.IGNORECASE),
        # Generic cloud/VM hostname patterns (with domain) - exclude 'node' to avoid re-masking
        re.compile(r'\b(?:vm|host|instance|server|worker|compute)[-_]?\d+\.[\w.-]+\b', re.IGNORECASE),
        # Generic cloud/VM hostname patterns (short form)
        re.compile(r'\b(?:vm|host|instance|server|worker|compute)[-_]?\d+\b', re.IGNORECASE),
        # EC2-style hostnames: ec2-X-X-X-X.region.compute.amazonaws.com
        re.compile(r'\bec2-\d+-\d+-\d+-\d+\.[\w.-]+\.amazonaws\.com\b', re.IGNORECASE),
        re.compile(r'\bec2-\d+-\d+-\d+-\d+\b', re.IGNORECASE),  # short form
        # smithi/testnode/mira patterns commonly used in Ceph testing
        re.compile(r'\b(?:smithi|testnode|mira|gibba|magna)\d*\.[\w.-]+\b', re.IGNORECASE),
        re.compile(r'\b(?:smithi|testnode|mira|gibba|magna)\d+\b', re.IGNORECASE),  # short form
        # RedHat PSI/RHOS lab domains (specific patterns, not generic redhat.com)
        re.compile(r'\b[\w-]+\.rhos-\d+\.[\w.-]+\.redhat\.com\b', re.IGNORECASE),
        re.compile(r'\b[\w-]+\.prod\.psi\.[\w.-]+\.redhat\.com\b', re.IGNORECASE),
    ]
    
    # Pattern to detect already-masked node IDs (to avoid re-masking)
    NODE_ID_RE = re.compile(r'^node\d+$', re.IGNORECASE)
    
    # Public domains that should NEVER be masked (common websites/services)
    PUBLIC_DOMAINS = {
        'ubuntu.com', 'canonical.com', 'launchpad.net',
        'redhat.com', 'centos.org', 'fedoraproject.org',
        'ceph.com', 'ceph.io', 
        'github.com', 'githubusercontent.com',
        'google.com', 'googleapis.com',
        'cloudfront.net', 'amazonaws.com', 'aws.amazon.com',
        'docker.io', 'docker.com', 'quay.io',
        'python.org', 'pypi.org',
    }
    
    def __init__(self, mask_ips=True, mask_ssh_keys=True, mask_hostnames=True,
                 allowed_ips=None, allowed_hosts=None):
        super().__init__()
        self.mask_ips = mask_ips
        self.mask_ssh_keys = mask_ssh_keys
        self.mask_hostnames = mask_hostnames
        self.allowed_ips = set(allowed_ips or ['127.0.0.1', '0.0.0.0'])
        self.allowed_hosts = set(allowed_hosts or ['localhost'])
        # Track hostname/IP to node mapping for consistent numbering
        self._host_map = OrderedDict()  # hostname -> node#
        self._ip_map = OrderedDict()    # IP -> node#
        self._node_counter = 0
    
    def _get_node_id(self, value, is_ip=False):
        """Get or create a consistent node ID for a hostname or IP."""
        # Skip if this is already a node ID
        if self.NODE_ID_RE.match(value):
            return value
        
        # For IPs, try to link to hostname if it matches ip-X-X-X-X pattern
        if is_ip:
            # Convert IP 10.0.196.28 to hostname pattern ip-10-0-196-28
            ip_as_hostname = 'ip-' + value.replace('.', '-')
            if ip_as_hostname in self._host_map:
                # Link this IP to the existing hostname's node
                self._ip_map[value] = self._host_map[ip_as_hostname]
                return f"node{self._ip_map[value]}"
            
            if value not in self._ip_map:
                self._node_counter += 1
                self._ip_map[value] = self._node_counter
            return f"node{self._ip_map[value]}"
        
        # For hostnames, extract base (first part before dot) for consistent mapping
        # This ensures ip-10-0-196-28 and ip-10-0-196-28.domain.com map to same node
        base = value.split('.')[0].lower()
        
        if base not in self._host_map:
            # Check if we already have an IP that matches this hostname
            # e.g., if hostname is ip-10-0-196-28, check if 10.0.196.28 exists
            if base.startswith('ip-'):
                ip_from_hostname = base[3:].replace('-', '.')
                if ip_from_hostname in self._ip_map:
                    # Link this hostname to the existing IP's node
                    self._host_map[base] = self._ip_map[ip_from_hostname]
                    return f"node{self._host_map[base]}"
            
            self._node_counter += 1
            self._host_map[base] = self._node_counter
        
        return f"node{self._host_map[base]}"
    
    def _mask(self, msg):
        """Apply all masks to message."""
        if not isinstance(msg, str):
            msg = str(msg)
        
        # Mask SSH private keys and key paths first
        if self.mask_ssh_keys:
            msg = self.SSH_PRIVKEY_RE.sub(self.SSH_KEY_MASK, msg)
            msg = self.SSH_KEY_RE.sub(self.SSH_KEY_MASK, msg)
            msg = self.SSH_KEY_PATH_RE.sub(self.KEY_PATH_MASK, msg)
        
        # Mask hostnames with consistent node IDs
        if self.mask_hostnames:
            for pattern in self.HOST_PATTERNS:
                def replace_host(m):
                    matched = m.group(0)
                    matched_lower = matched.lower()
                    # Skip if already masked
                    if self.NODE_ID_RE.match(matched):
                        return matched
                    # Skip if in allowed hosts list
                    if matched_lower in {h.lower() for h in self.allowed_hosts}:
                        return matched
                    # Extract base hostname for checks
                    base = matched_lower.split('.')[0]
                    # ALWAYS mask if it looks like a test node (ip-*, target*, ec2-*, etc.)
                    is_test_node = (
                        base.startswith('ip-') or
                        base.startswith('target') or
                        base.startswith('ec2-') or
                        base.startswith('smithi') or
                        base.startswith('testnode') or
                        base.startswith('mira') or
                        base.startswith('gibba') or
                        base.startswith('magna')
                    )
                    if not is_test_node:
                        # Skip if it's a public domain (not a test machine)
                        for domain in self.PUBLIC_DOMAINS:
                            if matched_lower.endswith('.' + domain) or matched_lower == domain:
                                return matched
                    return self._get_node_id(matched, is_ip=False)
                msg = pattern.sub(replace_host, msg)
        
        # Mask IPs with consistent node IDs
        if self.mask_ips:
            def replace_ip(m):
                matched = m.group(0)
                if matched in self.allowed_ips:
                    return matched
                return self._get_node_id(matched, is_ip=True)
            msg = self.IPV4_RE.sub(replace_ip, msg)
        
        return msg
    
    def _mask_arg(self, arg):
        """Mask an argument, converting to string first if needed."""
        if arg is None:
            return arg
        # Convert to string and mask - this handles objects with __str__
        # that may contain sensitive data (e.g., config objects)
        if isinstance(arg, str):
            return self._mask(arg)
        # For non-string types, convert to str, mask, and return as string
        # This is necessary to catch sensitive data in object representations
        try:
            str_arg = str(arg)
            masked = self._mask(str_arg)
            # Only return masked string if masking actually changed something
            if masked != str_arg:
                return masked
            return arg  # Return original if no masking needed
        except Exception:
            return arg

    def filter(self, record):
        """Filter and mask the log record."""
        # Mask the logger name (may contain hostname)
        if record.name:
            record.name = self._mask(record.name)
        if record.msg:
            record.msg = self._mask(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._mask_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._mask_arg(a) for a in record.args)
        return True


def enable_log_masking(mask_ips=True, mask_ssh_keys=True, mask_hostnames=True,
                       allowed_ips=None, allowed_hosts=None):
    """
    Enable real-time log masking on the root logger.
    
    Usage:
        from teuthology.util.logmask import enable_log_masking
        enable_log_masking()
    """
    global _active_filter
    
    filt = SensitiveDataFilter(
        mask_ips=mask_ips,
        mask_ssh_keys=mask_ssh_keys,
        mask_hostnames=mask_hostnames,
        allowed_ips=allowed_ips,
        allowed_hosts=allowed_hosts,
    )
    
    # Store globally so it can be applied to new handlers
    _active_filter = filt
    
    root = logging.getLogger()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
    
    return filt


def get_active_filter():
    """Return the active masking filter, if any."""
    return _active_filter


def apply_filter_to_handler(handler):
    """Apply the active masking filter to a handler, if masking is enabled."""
    if _active_filter is not None:
        handler.addFilter(_active_filter)
