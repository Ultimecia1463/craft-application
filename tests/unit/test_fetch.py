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
"""Tests for fetch-service-related functions."""
import re
import shlex
import subprocess
from pathlib import Path
from unittest import mock
from unittest.mock import call

import pytest
import responses
from craft_application import errors, fetch
from craft_providers.lxd import LXDInstance
from responses import matchers

CONTROL = fetch._DEFAULT_CONFIG.control
PROXY = fetch._DEFAULT_CONFIG.proxy
AUTH = fetch._DEFAULT_CONFIG.auth

assert_requests = responses.activate(assert_all_requests_are_fired=True)


@assert_requests
def test_get_service_status_success():
    responses.add(
        responses.GET,
        f"http://localhost:{CONTROL}/status",
        json={"uptime": 10},
        status=200,
    )
    status = fetch.get_service_status()
    assert status == {"uptime": 10}


@assert_requests
def test_get_service_status_failure():
    responses.add(
        responses.GET,
        f"http://localhost:{CONTROL}/status",
        status=404,
    )
    expected = "Error with fetch-service GET: 404 Client Error"
    with pytest.raises(errors.FetchServiceError, match=expected):
        fetch.get_service_status()


@pytest.mark.parametrize(
    ("status", "json", "expected"),
    [
        (200, {"uptime": 10}, True),
        (200, {"uptime": 10, "other-key": "value"}, True),
        (200, {"other-key": "value"}, False),
        (404, {"other-key": "value"}, False),
    ],
)
@assert_requests
def test_is_service_online(status, json, expected):
    responses.add(
        responses.GET,
        f"http://localhost:{CONTROL}/status",
        status=status,
        json=json,
    )
    assert fetch.is_service_online() == expected


def test_start_service(mocker, tmp_path):
    mock_is_online = mocker.patch.object(fetch, "is_service_online", return_value=False)
    mocker.patch.object(fetch, "_check_installed", return_value=True)
    mock_base_dir = mocker.patch.object(
        fetch, "_get_service_base_dir", return_value=tmp_path
    )
    mock_get_status = mocker.patch.object(
        fetch, "get_service_status", return_value={"uptime": 10}
    )
    mock_archive_key = mocker.patch.object(
        subprocess, "check_output", return_value="DEADBEEF"
    )

    fake_cert, fake_key = tmp_path / "cert.crt", tmp_path / "key.pem"
    mock_obtain_certificate = mocker.patch.object(
        fetch, "_obtain_certificate", return_value=(fake_cert, fake_key)
    )

    mock_popen = mocker.patch.object(subprocess, "Popen")
    mock_process = mock_popen.return_value
    mock_process.poll.return_value = None

    process = fetch.start_service()
    assert process is mock_process

    assert mock_is_online.called
    assert mock_base_dir.called
    assert mock_get_status.called
    mock_archive_key.assert_called_once_with(
        [
            "gpg",
            "--export",
            "--armor",
            "--no-default-keyring",
            "--keyring",
            "/snap/fetch-service/current/usr/share/keyrings/ubuntu-archive-keyring.gpg",
            "F6ECB3762474EDA9D21B7022871920D1991BC93C",
        ],
        text=True,
    )

    assert mock_obtain_certificate.called

    popen_call = mock_popen.mock_calls[0]
    assert popen_call == call(
        [
            "bash",
            "-c",
            shlex.join(
                [
                    fetch._FETCH_BINARY,
                    f"--control-port={CONTROL}",
                    f"--proxy-port={PROXY}",
                    f"--config={tmp_path/'config'}",
                    f"--spool={tmp_path/'spool'}",
                    f"--cert={fake_cert}",
                    f"--key={fake_key}",
                    "--permissive-mode",
                    "--idle-shutdown=300",
                ]
            )
            + f" > {fetch._get_log_filepath()}",
        ],
        env={"FETCH_SERVICE_AUTH": AUTH, "FETCH_APT_RELEASE_PUBLIC_KEY": "DEADBEEF"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def test_start_service_already_up(mocker):
    """If the fetch-service is already up then a new process is *not* created."""
    mock_is_online = mocker.patch.object(fetch, "is_service_online", return_value=True)
    mock_popen = mocker.patch.object(subprocess, "Popen")

    assert fetch.start_service() is None

    assert mock_is_online.called
    assert not mock_popen.called


def test_start_service_not_installed(mocker):
    mocker.patch.object(fetch, "is_service_online", return_value=False)
    mocker.patch.object(fetch, "_check_installed", return_value=False)

    expected = re.escape("The 'fetch-service' snap is not installed.")
    with pytest.raises(errors.FetchServiceError, match=expected):
        fetch.start_service()


@assert_requests
def test_create_session():
    responses.add(
        responses.POST,
        f"http://localhost:{CONTROL}/session",
        json={"id": "my-session-id", "token": "my-session-token"},
        status=200,
        match=[matchers.json_params_matcher({"policy": "permissive"})],
    )

    session_data = fetch.create_session()

    assert session_data.session_id == "my-session-id"
    assert session_data.token == "my-session-token"


@assert_requests
def test_teardown_session():
    session_data = fetch.SessionData(id="my-session-id", token="my-session-token")

    # Call to delete token
    responses.delete(
        f"http://localhost:{CONTROL}/session/{session_data.session_id}/token",
        match=[matchers.json_params_matcher({"token": session_data.token})],
        json={},
        status=200,
    )
    # Call to get session report
    responses.get(
        f"http://localhost:{CONTROL}/session/{session_data.session_id}",
        json={},
        status=200,
    )
    # Call to delete session
    responses.delete(
        f"http://localhost:{CONTROL}/session/{session_data.session_id}",
        json={},
        status=200,
    )
    # Call to delete session resources
    responses.delete(
        f"http://localhost:{CONTROL}/resources/{session_data.session_id}",
        json={},
        status=200,
    )

    fetch.teardown_session(session_data)


def test_configure_build_instance(mocker):
    mocker.patch.object(fetch, "_get_gateway", return_value="127.0.0.1")
    mocker.patch.object(
        fetch, "_obtain_certificate", return_value=("fake-cert.crt", "key.pem")
    )

    session_data = fetch.SessionData(id="my-session-id", token="my-session-token")
    instance = mock.MagicMock(spec_set=LXDInstance)
    assert isinstance(instance, LXDInstance)

    expected_proxy = f"http://my-session-id:my-session-token@127.0.0.1:{PROXY}/"
    expected_env = {
        "http_proxy": expected_proxy,
        "https_proxy": expected_proxy,
        "REQUESTS_CA_BUNDLE": "/usr/local/share/ca-certificates/local-ca.crt",
        "CARGO_HTTP_CAINFO": "/usr/local/share/ca-certificates/local-ca.crt",
    }

    env = fetch.configure_instance(instance, session_data)
    assert env == expected_env

    # Execution calls on the instance
    assert instance.execute_run.mock_calls == [
        call(
            ["/bin/sh", "-c", "/usr/sbin/update-ca-certificates > /dev/null"],
            check=True,
        ),
        call(["mkdir", "-p", "/root/.pip"]),
        call(["systemctl", "restart", "snapd"]),
        call(
            [
                "snap",
                "set",
                "system",
                f"proxy.http={expected_proxy}",
            ]
        ),
        call(
            [
                "snap",
                "set",
                "system",
                f"proxy.https={expected_proxy}",
            ]
        ),
        call(["/bin/rm", "-Rf", "/var/lib/apt/lists"], check=True),
        call(
            ["apt", "update"],
            env=expected_env,
            check=True,
            stdout=mocker.ANY,
            stderr=mocker.ANY,
        ),
    ]

    # Files pushed to the instance
    assert instance.push_file.mock_calls == [
        call(
            source="fake-cert.crt",
            destination=Path("/usr/local/share/ca-certificates/local-ca.crt"),
        )
    ]

    assert instance.push_file_io.mock_calls == [
        call(
            destination=Path("/root/.pip/pip.conf"),
            content=mocker.ANY,
            file_mode="0644",
        ),
        call(
            destination=Path("/etc/apt/apt.conf.d/99proxy"),
            content=mocker.ANY,
            file_mode="0644",
        ),
    ]


def test_get_certificate_dir(mocker):
    mocker.patch.object(
        fetch,
        "_get_service_base_dir",
        return_value=Path("/home/user/snap/fetch-service/common"),
    )
    cert_dir = fetch._get_certificate_dir()

    expected = Path("/home/user/snap/fetch-service/common/craft/fetch-certificate")
    assert cert_dir == expected
