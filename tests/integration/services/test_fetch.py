#  This file is part of craft-application.
#
#  Copyright 2024 Canonical Ltd.
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Lesser General Public License version 3, as
#  published by the Free Software Foundation.
#
#  This program is distributed in the hope that it will be useful, but WITHOUT
#  ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
#  SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.
#  See the GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Tests for FetchService."""
import contextlib
import io
import pathlib
import shutil
import socket
import textwrap
from functools import cache
from unittest import mock

import craft_providers
import pytest
from craft_application import errors, fetch, services, util
from craft_application.models import BuildInfo
from craft_providers import bases


@cache
def _get_fake_certificate_dir():
    base_dir = fetch._get_service_base_dir()

    return base_dir / "test-craft-app/fetch-certificate"


@pytest.fixture(autouse=True, scope="session")
def _set_test_certificate_dir():
    """A session-scoped fixture so that we generate the certificate only once"""
    cert_dir = _get_fake_certificate_dir()
    if cert_dir.is_dir():
        shutil.rmtree(cert_dir)

    with mock.patch.object(fetch, "_get_certificate_dir", return_value=cert_dir):
        fetch._obtain_certificate()

    yield

    shutil.rmtree(cert_dir)


@pytest.fixture(autouse=True)
def _set_test_base_dirs(mocker):
    original = fetch._get_service_base_dir()
    test_dir = original / "test"
    test_dir.mkdir(exist_ok=True)
    mocker.patch.object(fetch, "_get_service_base_dir", return_value=test_dir)

    cert_dir = _get_fake_certificate_dir()
    mocker.patch.object(fetch, "_get_certificate_dir", return_value=cert_dir)

    yield

    shutil.rmtree(test_dir)


@pytest.fixture()
def app_service(app_metadata, fake_services):
    fetch_service = services.FetchService(app_metadata, fake_services)
    yield fetch_service
    fetch_service.shutdown(force=True)


def test_start_service(app_service):
    assert not fetch.is_service_online()
    app_service.setup()
    assert fetch.is_service_online()


def test_start_service_already_up(app_service, request):
    # Create a fetch-service "manually"
    fetch_process = fetch.start_service()
    assert fetch.is_service_online()
    # Ensure its cleaned up when the test is done
    if fetch_process is not None:
        request.addfinalizer(lambda: fetch.stop_service(fetch_process))

    app_service.setup()
    assert fetch.is_service_online()


@pytest.mark.parametrize(
    "port", [fetch._DEFAULT_CONFIG.control, fetch._DEFAULT_CONFIG.proxy]
)
def test_start_service_port_taken(app_service, request, port):
    # "Occupy" one of the necessary ports manually.
    soc = socket.create_server(("localhost", port), reuse_port=True)
    request.addfinalizer(soc.close)

    assert not fetch.is_service_online()

    proxy = fetch._DEFAULT_CONFIG.proxy
    control = fetch._DEFAULT_CONFIG.control

    expected = f"fetch-service ports {proxy} and {control} are already in use."
    with pytest.raises(errors.FetchServiceError, match=expected):
        app_service.setup()


def test_shutdown_service(app_service):
    assert not fetch.is_service_online()

    app_service.setup()
    assert fetch.is_service_online()

    # By default, shutdown() without parameters doesn't actually stop the
    # fetch-service.
    app_service.shutdown()
    assert fetch.is_service_online()

    # shutdown(force=True) must stop the fetch-service.
    app_service.shutdown(force=True)
    assert not fetch.is_service_online()


def test_create_teardown_session(app_service, mocker):
    mocker.patch.object(fetch, "_get_gateway", return_value="127.0.0.1")
    app_service.setup()

    assert len(fetch.get_service_status()["active-sessions"]) == 0

    app_service.create_session(
        instance=mock.MagicMock(spec_set=craft_providers.Executor)
    )
    assert len(fetch.get_service_status()["active-sessions"]) == 1

    report = app_service.teardown_session()
    assert len(fetch.get_service_status()["active-sessions"]) == 0

    assert "artefacts" in report


# Bash script to setup the build instance before the actual testing.
setup_environment = (
    textwrap.dedent(
        """
    #! /bin/bash
    set -euo pipefail

    apt install -y python3.10-venv
    python3 -m venv venv
    venv/bin/pip install requests
"""
    )
    .strip()
    .encode("ascii")
)

wheel_url = (
    "https://files.pythonhosted.org/packages/0f/ec/"
    "a9b769274512ea65d8484c2beb8c3d2686d1323b450ce9ee6d09452ac430/"
    "craft_application-3.0.0-py3-none-any.whl"
)
# Bash script to fetch the craft-application wheel.
check_requests = (
    textwrap.dedent(
        f"""
    #! /bin/bash
    set -euo pipefail

    venv/bin/python -c "import requests; requests.get('{wheel_url}').raise_for_status()"
"""
    )
    .strip()
    .encode("ascii")
)


@pytest.fixture()
def lxd_instance(snap_safe_tmp_path, provider_service):
    provider_service.get_provider("lxd")

    arch = util.get_host_architecture()
    build_info = BuildInfo("foo", arch, arch, bases.BaseName("ubuntu", "22.04"))
    instance = provider_service.instance(build_info, work_dir=snap_safe_tmp_path)

    with instance as executor:
        executor.push_file_io(
            destination=pathlib.Path("/root/setup-environment.sh"),
            content=io.BytesIO(setup_environment),
            file_mode="0644",
        )
        executor.execute_run(
            ["bash", "/root/setup-environment.sh"],
            check=True,
            capture_output=True,
        )
        yield executor

    if executor is not None:
        with contextlib.suppress(craft_providers.ProviderError):
            executor.delete()


def test_build_instance_integration(app_service, lxd_instance):
    app_service.setup()

    env = app_service.create_session(lxd_instance)

    try:
        # Install the hello Ubuntu package.
        lxd_instance.execute_run(
            ["apt", "install", "-y", "hello"], check=True, env=env, capture_output=True
        )

        # Download the craft-application wheel.
        lxd_instance.push_file_io(
            destination=pathlib.Path("/root/check-requests.sh"),
            content=io.BytesIO(check_requests),
            file_mode="0644",
        )
        lxd_instance.execute_run(
            ["bash", "/root/check-requests.sh"],
            check=True,
            env=env,
            capture_output=True,
        )
    finally:
        report = app_service.teardown_session()

    artefacts_and_types: list[tuple[str, str]] = []

    for artefact in report["artefacts"]:
        metadata_name = artefact["metadata"]["name"]
        metadata_type = artefact["metadata"]["type"]

        artefacts_and_types.append((metadata_name, metadata_type))

    # Check that the installation of the "hello" deb went through the inspector.

    # NOTE: the right type is missing on deb artefacts currently - the "type"
    # field is empty. If this fails, set "application/vnd.debian.binary-package"
    # instead of "".
    assert ("hello", "") in artefacts_and_types

    # Check that the fetching of the "craft-application" wheel went through the inspector.
    assert ("craft-application", "application/x.python.wheel") in artefacts_and_types