import os

import pytest

# Ansible configs outside the test's own folders: a host with ansible installed has one, and
# the shipped rule would read it.
_HOST_ANSIBLE_CFGS = {"/etc/ansible/ansible.cfg", os.path.expanduser("~/.ansible.cfg")}


@pytest.fixture(autouse=True)
def _no_host_ansible_cfg(monkeypatch):
    isfile = os.path.isfile
    monkeypatch.setattr(
        os.path, "isfile", lambda p: os.fspath(p) not in _HOST_ANSIBLE_CFGS and isfile(p)
    )
