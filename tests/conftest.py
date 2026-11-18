import pytest

from joshy.config import Settings
from joshy.db import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, password="pw", timezone="America/New_York")


@pytest.fixture
def db(settings):
    return Database(settings.data_dir)
