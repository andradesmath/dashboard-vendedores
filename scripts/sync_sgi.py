"""
scripts/sync_sgi.py - Sincroniza as vendas do dia a partir do SGI Solution
(login + relatório "Totais de Vendas") direto no Postgres do painel, seguindo o
roteiro: Login -> Vendas -> Totais de Vendas -> período = hoje -> gerar PDF ->
extrair Nº Ped e T. Liq. por vendedor cadastrado.

Pensado para rodar via GitHub Actions (agendado às 12h e 19h de Brasília), mas
roda local também para testar/depurar.

IMPORTANTE: como não foi possível inspecionar o site ao vivo durante o
desenvolvimento (a página trava carregando na sessão usada para construir isso, e
por política eu não insiro senha em formulários de qualquer forma), os seletores
abaixo foram escritos com base nas TELAS do roteiro em PDF enviado, com múltiplas
estratégias de fallback e captura de screenshot/HTML a cada etapa em caso de erro.
É bem provável que a primeira execução precise de um ajuste fino — rode com
`--headed` localmente ou dispare o workflow manualmente (workflow_dispatch) no
GitHub Actions e envie os screenshots salvos em debug_*.png se algo falhar.

Variáveis de ambiente necessárias (Secrets no GitHub, ou .env local para teste):
    DATABASE_URL            - mesma connection string do painel (Neon/Postgres)
    SGI_URL_LOGIN           - ex.: http://138.255.35.101:8888/login
    SGI_LOGIN               - usuário do SGI
    SGI_SENHA               - senha do SGI
    SGI_EMPRESA_PORTEIRA    - texto exato da opção "Empresa" no login p/ Porteira
                              (confirmado: "PORTEIRA AGROCOMERCIAL")
    SGI_EMPRESA_CASA_ADUBO  - texto exato da opção "Empresa" no login p/ Casa de
                              Adubo (confirme no dropdown do site)
Só sincroniza as lojas cuja variável SGI_EMPRESA_* estiver definida.

Uso:
    python scripts/sync_sgi.py                 # roda para todas as lojas configuradas
    python scripts/sync_sgi.py --loja Porteira  # roda só uma loja (debug)
    python scripts/sync_sgi.py --headed         # abre o navegador visível (debug local)
"""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db  # noqa: E402
from sgi_relatorio import casar_vendedores, parse_relatorio_vendas_pdf  # noqa: E402

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


LOJA_PARA_EMPRESA_ENV = {
    "Porteira": "SGI_EMPRESA_PORTEIRA",
    "Casa de Adubo": "SGI_EMPRESA_CASA_ADUBO",
}


def obter_lojas_configuradas():
    return [loja for loja, env_var in LOJA_PARA_EMPRESA_ENV.items() if os.environ.get(env_var)]


def _salvar_debug(page, loja, etapa):
    """Salva screenshot + HTML da página no ponto onde algo deu errado, pra dar pra
    diagnosticar sem precisar acessar o site ao vivo de novo."""
    prefixo = f"debug_{loja.replace(' ', '_')}_{etapa}"
    try:
        page.screenshot(path=f"{prefixo}.png", full_page=True)
        with open(f"{prefixo}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"  [debug] salvo {prefixo}.png / {prefixo}.html")
    except Exception as e:
        print(f"  [debug] não foi possível salvar debug da etapa '{etapa}': {e}")


def _preencher_por_label_ou_placeholder(page, rotulos, valor):
    """Tenta preencher um campo por várias estratégias (label acessível, placeholder,
    ou o input mais próximo de um texto visível), na ordem — os formulários desse
    tipo de sistema legado nem sempre expõem <label for=...> corretamente."""
    for rotulo in rotulos:
        for tentativa in (
            lambda: page.get_by_label(rotulo, exact=False),
            lambda: page.get_by_placeholder(rotulo, exact=False),
        ):
            try:
                campo = tentativa()
                if campo.count() > 0:
                    campo.first.fill(valor)
                    return True
            except Exception:
                continue
    return False


def logar_e_baixar_pdf(playwright, loja, url_login, login, senha, empresa_texto, headed=False):
    """Abre o navegador, loga no SGI, navega até Vendas > Totais de Vendas, ajusta o
    período pro dia de hoje e captura o PDF gerado. Retorna os bytes do PDF."""
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    try:
        # ---- 1) Login ----
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)  # dá tempo de qualquer JS inicial rodar

        try:
            page.get_by_label("Empresa", exact=False).select_option(label=empresa_texto)
        except Exception:
            # fallback: primeiro <select> da página
            page.locator("select").first.select_option(label=empresa_texto)

        if not _preencher_por_label_ou_placeholder(page, ["Login", "Usuário", "Usuario"], login):
            page.locator("input[type='text']").first.fill(login)
        if not _preencher_por_label_ou_placeholder(page, ["Senha"], senha):
            page.locator("input[type='password']").first.fill(senha)

        _salvar_debug(page, loja, "antes_do_login")
        page.get_by_role("button", name="Login").click()
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _salvar_debug(page, loja, "depois_do_login")

        # ---- 2) Sidebar: Vendas > Totais de Vendas ----
        page.get_by_text("Vendas", exact=True).first.click()
        page.wait_for_timeout(500)
        page.get_by_text("Totais de Vendas", exact=True).first.click()
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _salvar_debug(page, loja, "form_relatorio")

        # ---- 3) Período = hoje até hoje ----
        hoje_str = date.today().strftime("%d/%m/%Y")
        campos_periodo = page.locator("text=Período").locator("xpath=following::input").all()
        if len(campos_periodo) >= 2:
            campos_periodo[0].fill(hoje_str)
            campos_periodo[1].fill(hoje_str)
        else:
            print(f"  [{loja}] aviso: não achei os 2 campos de período — o relatório pode sair "
                  "com a data padrão do formulário em vez de hoje.")

        # ---- 4) Gerar e capturar o PDF ----
        botao_pdf = page.get_by_role("button", name="PDF", exact=False)

        # Estratégia A: o clique dispara um download de verdade.
        try:
            with page.expect_download(timeout=10000) as download_info:
                botao_pdf.click()
            caminho_tmp = download_info.value.path()
            with open(caminho_tmp, "rb") as f:
                return f.read()
        except PlaywrightTimeoutError:
            pass

        # Estratégia B: o clique abre uma NOVA ABA com o PDF (visualizador do Chrome
        # ou uma URL que responde application/pdf).
        try:
            with context.expect_page(timeout=10000) as nova_pagina_info:
                botao_pdf.click()
            nova_pagina = nova_pagina_info.value
            nova_pagina.wait_for_load_state("domcontentloaded", timeout=30000)
            resposta = nova_pagina.request.get(nova_pagina.url) if hasattr(nova_pagina, "request") else None
            if resposta is not None:
                return resposta.body()
            # fallback final: pede pro Chromium exportar a própria aba como PDF
            return nova_pagina.pdf()
        except PlaywrightTimeoutError:
            pass

        # Estratégia C: a resposta do clique é interceptada como uma resposta de
        # rede com content-type PDF, sem abrir aba nova nem virar "download".
        with page.expect_response(
            lambda r: "pdf" in (r.headers.get("content-type", "").lower()) or r.url.lower().endswith(".pdf"),
            timeout=15000,
        ) as resposta_info:
            botao_pdf.click()
        return resposta_info.value.body()

    except Exception:
        _salvar_debug(page, loja, "erro")
        raise
    finally:
        browser.close()


def sincronizar_loja(playwright, loja, hoje, headed=False):
    url_login = os.environ["SGI_URL_LOGIN"]
    login = os.environ["SGI_LOGIN"]
    senha = os.environ["SGI_SENHA"]
    empresa_texto = os.environ[LOJA_PARA_EMPRESA_ENV[loja]]

    print(f"[{loja}] Fazendo login e gerando relatório de {hoje.strftime('%d/%m/%Y')}...")
    pdf_bytes = logar_e_baixar_pdf(playwright, loja, url_login, login, senha, empresa_texto, headed=headed)

    resultado = parse_relatorio_vendas_pdf(pdf_bytes)
    if not resultado["linhas"]:
        with open(f"debug_{loja.replace(' ', '_')}_pdf_vazio.pdf", "wb") as f:
            f.write(pdf_bytes)
        print(f"[{loja}] Nenhuma linha de vendedor reconhecida no PDF gerado "
              f"(salvo em debug_{loja.replace(' ', '_')}_pdf_vazio.pdf para inspeção). Pulando essa loja.")
        return

    vendedores_df = db.get_vendedores(loja=loja, apenas_ativos=True)
    matched, nao_encontrados = casar_vendedores(resultado["linhas"], vendedores_df)

    if nao_encontrados:
        print(f"[{loja}] Ignorados (sem correspondência no cadastro): {', '.join(nao_encontrados)}")

    for item in matched:
        db.upsert_venda(item["vendedor_id"], hoje, float(item["t_liq"]), int(item["n_ped"]))
        print(f"[{loja}] {item['nome']}: {item['n_ped']} pedido(s), R$ {item['t_liq']:.2f}")

    print(f"[{loja}] {len(matched)} vendedor(es) sincronizado(s) para {hoje.strftime('%d/%m/%Y')}.")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza vendas do dia a partir do SGI Solution.")
    parser.add_argument("--loja", choices=list(LOJA_PARA_EMPRESA_ENV.keys()), help="Rodar só uma loja (debug).")
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (só faz sentido local).")
    args = parser.parse_args()

    hoje = date.today()
    lojas = [args.loja] if args.loja else obter_lojas_configuradas()
    if not lojas:
        print(
            "Nenhuma loja configurada — defina SGI_EMPRESA_PORTEIRA e/ou "
            "SGI_EMPRESA_CASA_ADUBO (ou passe --loja)."
        )
        sys.exit(1)

    erros = []
    with sync_playwright() as playwright:
        for loja in lojas:
            try:
                sincronizar_loja(playwright, loja, hoje, headed=args.headed)
            except Exception as e:
                erros.append(f"{loja}: {e}")
                print(f"[{loja}] ERRO: {e}")

    if erros:
        print("\nFalhas nessa execução:")
        for erro in erros:
            print(f"  - {erro}")
        sys.exit(1)

    print("\nSincronização concluída sem erros.")


if __name__ == "__main__":
    main()