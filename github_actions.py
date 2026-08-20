"""
github_actions.py - Dispara e acompanha, pela API do GitHub, o workflow que
sincroniza os dados do SGI (scripts/sync_sgi.py via .github/workflows/sync_sgi.yml)
— dá pra rodar a sincronização direto de um botão no painel em vez de precisar
abrir o GitHub ou rodar o script na mão.

Por que o painel não roda o Playwright direto: o Streamlit Community Cloud não dá
pra instalar/rodar um navegador Chromium de verdade de forma confiável (ambiente
restrito, sem persistência entre reinícios, sessão dorme sozinha). O GitHub Actions
já tem isso funcionando numa VM completa. Então o botão do painel só aciona à
DISTÂNCIA o mesmo workflow que já roda sozinho às 12h/19h, e fica acompanhando até
ele terminar — o painel então limpa o cache e atualiza os números.

Requer o secret GITHUB_TOKEN (um Personal Access Token do GitHub, com permissão
"Actions: Read and write" no repositório andradesmath/dashboard-vendedores) — no
mesmo lugar do DATABASE_URL: variável de ambiente/.env local, ou st.secrets no
Streamlit Cloud.
"""
import os
import time
from datetime import datetime, timezone

import requests

REPO = "andradesmath/dashboard-vendedores"
WORKFLOW_ARQUIVO = "sync_sgi.yml"
API_BASE = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_ARQUIVO}"


class SincronizacaoIndisponivel(Exception):
    """Erro esperado (config faltando, API indisponível) — tratado na tela sem
    traceback, diferente de um bug de verdade."""


def _token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        import streamlit as st
        if "GITHUB_TOKEN" in st.secrets:
            return st.secrets["GITHUB_TOKEN"]
    except Exception:
        pass
    raise SincronizacaoIndisponivel(
        "Sincronização pelo painel ainda não configurada: falta o secret GITHUB_TOKEN "
        "(Personal Access Token do GitHub com permissão 'Actions: Read and write' "
        "no repositório). Defina em .env local ou nas Secrets do Streamlit Cloud."
    )


def _headers():
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def disparar_sincronizacao(loja=None, data=None, ref="main"):
    """Dispara o workflow sync_sgi.yml (workflow_dispatch) pra rodar agora — ou
    pra uma data específica, se `data` for informado (string "DD/MM/AAAA"; padrão
    é hoje, quando None). Retorna o instante (UTC) do disparo — usado depois pra
    achar a execução certa entre as runs recentes do workflow."""
    disparado_em = datetime.now(timezone.utc)
    inputs = {}
    if loja:
        inputs["loja"] = loja
    if data:
        inputs["data"] = data
    try:
        resp = requests.post(
            f"{API_BASE}/dispatches",
            headers=_headers(),
            json={"ref": ref, "inputs": inputs},
            timeout=20,
        )
    except requests.RequestException as e:
        raise SincronizacaoIndisponivel(f"Não consegui falar com a API do GitHub: {e}")

    if resp.status_code != 204:
        raise SincronizacaoIndisponivel(
            f"Falha ao disparar a sincronização no GitHub Actions (HTTP {resp.status_code}): "
            f"{resp.text[:300]}"
        )
    return disparado_em


def _achar_run_disparada(disparado_em):
    resp = requests.get(
        f"{API_BASE}/runs",
        headers=_headers(),
        params={"event": "workflow_dispatch", "per_page": 5},
        timeout=20,
    )
    resp.raise_for_status()
    for run in resp.json().get("workflow_runs", []):
        criada_em = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if criada_em >= disparado_em:
            return run
    return None


def aguardar_conclusao(disparado_em, timeout_s=280, intervalo_s=6, callback_status=None):
    """Espera a execução disparada em `disparado_em` terminar, dando poll na API do
    GitHub Actions. `callback_status(texto)` é chamado a cada checagem, se
    informado (pra atualizar um st.spinner/st.caption na tela).

    Retorna (sucesso, url_run):
      sucesso=True  -> terminou com sucesso.
      sucesso=False -> terminou, mas com erro (ver url_run pros logs).
      sucesso=None  -> não deu tempo de confirmar dentro do timeout (pode ainda
                       estar rodando — não é necessariamente uma falha)."""
    inicio = time.monotonic()
    run = None
    while run is None and time.monotonic() - inicio < timeout_s:
        run = _achar_run_disparada(disparado_em)
        if run is None:
            if callback_status:
                callback_status("Aguardando o GitHub Actions iniciar a sincronização...")
            time.sleep(intervalo_s)

    if run is None:
        raise SincronizacaoIndisponivel(
            "Não encontrei a execução disparada no GitHub Actions depois de alguns "
            "segundos — confira manualmente na aba Actions do repositório."
        )

    url_run = run.get("html_url")
    while True:
        resp = requests.get(run["url"], headers=_headers(), timeout=20)
        resp.raise_for_status()
        run = resp.json()
        status = run.get("status")
        if callback_status:
            callback_status(f"Status no GitHub Actions: {status}...")
        if status == "completed":
            return run.get("conclusion") == "success", url_run
        if time.monotonic() - inicio > timeout_s:
            return None, url_run
        time.sleep(intervalo_s)