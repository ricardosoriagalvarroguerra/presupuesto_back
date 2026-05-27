import asyncio

import pytest


@pytest.mark.asyncio
async def test_login_ok_devuelve_token_y_user(client):
    r = await client.post("/auth/login",
                          json={"usuario": "mmednik", "password": "Matias2026!"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "access_token" in data and len(data["access_token"]) > 100
    assert data["token_type"] == "bearer"
    assert data["scope"] == "full"
    assert data["requiere_cambio_password"] is False
    u = data["user"]
    assert u["username"] == "mmednik"
    assert u["vp_codigo"] == "VPF"
    assert u["ver_todo"] is True
    assert "vicepresidente" in u["roles"]


@pytest.mark.asyncio
async def test_login_password_incorrecta_da_401_generico(client):
    r = await client.post("/auth/login",
                          json={"usuario": "mmednik", "password": "wrong!!"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Usuario o contraseña inválidos"


@pytest.mark.asyncio
async def test_login_usuario_inexistente_da_misma_respuesta(client):
    """No debe filtrar si el usuario existe — misma respuesta que password mala."""
    r = await client.post("/auth/login",
                          json={"usuario": "no.existe@nada", "password": "wrong!!"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Usuario o contraseña inválidos"


@pytest.mark.asyncio
async def test_login_body_vacio_da_422(client):
    r = await client.post("/auth/login", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_rechaza_campos_extra(client):
    r = await client.post("/auth/login",
                          json={"usuario": "x", "password": "y", "extra": "z"})
    assert r.status_code == 422
    assert any(e["type"] == "extra_forbidden" for e in r.json()["detail"])


@pytest.mark.asyncio
async def test_me_sin_token_da_401(client):
    r = await client.get("/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_con_token_invalido_da_401(client):
    r = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_ok_devuelve_usuario(client, token_vpf_todo):
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {token_vpf_todo}"})
    assert r.status_code == 200
    assert r.json()["username"] == "mmednik"
