# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy

import pytest

from rp_core.demo import EVENT, initialize_instance
from rp_core.reference import SQLiteAdapter


@pytest.fixture
def event():
    return copy.deepcopy(EVENT)


@pytest.fixture
def instance(tmp_path):
    root = tmp_path / "private"
    initialize_instance(root)
    return root


@pytest.fixture
def adapter(instance):
    adapter = SQLiteAdapter(instance)
    yield adapter
    adapter.close()
