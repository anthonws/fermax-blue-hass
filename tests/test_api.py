"""Tests for the Fermax Blue API client."""

from unittest.mock import patch

import httpx
import pytest

from custom_components.fermax_blue.api import (
    FermaxAuthError,
    FermaxBlueApi,
)


@pytest.fixture
def api():
    """Return a FermaxBlueApi instance."""
    return FermaxBlueApi("test@example.com", "testpass123")


class TestAuthentication:
    """Test authentication flow."""

    @pytest.mark.asyncio
    async def test_successful_auth(self, api):
        """Test successful authentication."""
        mock_response = httpx.Response(
            200,
            json={
                "access_token": "test_token_123",
                "expires_in": 3600,
                "token_type": "bearer",
            },
            request=httpx.Request("POST", "https://test.com"),
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            token = await api.authenticate()

        assert token == "test_token_123"
        assert api.is_authenticated

    @pytest.mark.asyncio
    async def test_invalid_credentials(self, api):
        """Test authentication with invalid credentials."""
        mock_response = httpx.Response(
            401,
            json={
                "error": "invalid_grant",
                "error_description": "Bad credentials",
            },
            request=httpx.Request("POST", "https://test.com"),
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            with pytest.raises(FermaxAuthError, match="Bad credentials"):
                await api.authenticate()

    @pytest.mark.asyncio
    async def test_token_expiry(self, api):
        """Test that expired tokens trigger re-authentication."""
        import time

        api._access_token = "old_token"
        api._token_expires_at = time.time() - 100  # Expired

        assert not api.is_authenticated


class TestPairings:
    """Test pairing retrieval."""

    @pytest.mark.asyncio
    async def test_get_pairings(self, api):
        """Test fetching paired devices."""
        api._access_token = "valid_token"
        api._token_expires_at = 9999999999

        mock_response = httpx.Response(
            200,
            json=[
                {
                    "deviceId": "device_123",
                    "tag": "My Home",
                    "installationId": "inst_001",
                    "accessDoorMap": {
                        "GENERAL": {
                            "title": "Portal",
                            "accessId": {
                                "block": 100,
                                "subblock": -1,
                                "number": 0,
                            },
                            "visible": True,
                        }
                    },
                }
            ],
            request=httpx.Request("GET", "https://test.com"),
        )

        with patch("httpx.AsyncClient.get", return_value=mock_response):
            pairings = await api.get_pairings()

        assert len(pairings) == 1
        assert pairings[0].device_id == "device_123"
        assert pairings[0].tag == "My Home"
        assert "GENERAL" in pairings[0].access_doors
        assert pairings[0].access_doors["GENERAL"].visible is True


class TestDoorControl:
    """Test door opening."""

    @pytest.mark.asyncio
    async def test_open_door(self, api):
        """Test successful door opening."""
        api._access_token = "valid_token"
        api._token_expires_at = 9999999999

        mock_response = httpx.Response(
            200,
            text="la puerta abierta",
            request=httpx.Request("POST", "https://test.com"),
        )

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            result = await api.open_door(
                "device_123",
                {"block": 100, "subblock": -1, "number": 0},
            )

        assert result is True
