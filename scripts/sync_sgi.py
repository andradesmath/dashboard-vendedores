"""
scripts/sync_sgi.py - Sincroniza as vendas do dia a partir do SGI Solution direto
no Postgres do painel, puxando DOIS relatórios na mesma sessão logada:
  1) "Totais de Vendas" -> Nº Ped e T. Liq. por vendedor (vendas_diarias).
  2) "Totais de Vendas Por Produto" -> produtos vendidos por vendedor, com
     marca/fornecedor (vendas_produtos_diarias) — agrupamento sempre
     "Vendedor/Produto".

Roteiro de cada um: Login -> Vendas -> <relatório> -> período = hoje -> gerar
PDF -> extrair dados.

Pensado para rodar via GitHub Actions (agendado às 12h e 19h de Brasília), mas
roda local também para testar/depurar.

IMPORTANTE: como não foi possível inspecionar o site ao vivo durante o
desenvolvimento (a página trava carregando na sessão usada para construir isso, e
por política eu não insiro senha em formulários de qualquer forma), os seletores
abaixo foram escritos com base nas TELAS dos roteiros em PDF enviados, com
múltiplas estratégias de fallback e captura de screenshot/HTML a cada etapa em
caso de erro. O parser do relatório de produtos foi validado direto contra um PDF
real gerado pelo usuário (bate 100% com os totais impressos); a NAVEGAÇÃO até esse
relatório (cliques, campo "Agrupamento") ainda não foi testada ao vivo — rode com
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
                              Adubo (confirmado: "CASA DE ADUBOS CAFE BOM")
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

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    # Caminho explícito (não depende de busca automática por diretório de
    # trabalho/pilha de chamadas) — o .env sempre fica na raiz do projeto, um
    # nível acima da pasta scripts/.
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

import db  # noqa: E402
from sgi_relatorio import (  # noqa: E402
    casar_produtos_vendedores,
    casar_vendedores,
    parse_relatorio_produtos_pdf,
    parse_relatorio_vendas_pdf,
)

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


def selecionar_empresa_no_formulario(page, empresa_texto):
    """Seleciona a empresa no formulário 'Totais de Vendas'. É um <select> nativo
    (confirmado pelo log de erro de uma tentativa anterior: a opção resolvia para
    '<option value="...">CASA DE ADUBOS CAFE BOM</option>' dentro de um <select> —
    o visual de "caixa de busca" era o próprio navegador/SO renderizando um select
    nativo com muitas opções, não um componente customizado). select_option() no
    <select> que contém essa opção resolve direto, sem precisar clicar em nada."""
    select_locator = page.locator(f"select:has(option:text-is('{empresa_texto}'))").first
    if select_locator.count() == 0:
        select_locator = page.get_by_label("Empresa", exact=False).first
    select_locator.select_option(label=empresa_texto, timeout=10000)


def selecionar_agrupamento_vendedor_produto(page):
    """Garante que o campo 'Agrupamento' do formulário 'Totais de Vendas Por
    Produto' esteja em 'Vendedor/Produto' — exigência explícita do usuário, é o
    agrupamento que faz o relatório trazer uma seção por vendedor com os produtos
    dele (sem isso o relatório sai no formato errado pro parser). Tenta como
    <select> nativo primeiro (mesmo padrão do campo 'Empresa' do outro relatório);
    se não for, trata como dropdown customizado: clica no campo e depois na opção
    pelo texto (como no print enviado pelo usuário, que mostra uma lista com
    'Vendedor/Produto' já marcado com um check vermelho)."""
    valor = "Vendedor/Produto"
    try:
        select_locator = page.locator(f"select:has(option:text-is('{valor}'))").first
        if select_locator.count() > 0:
            select_locator.select_option(label=valor, timeout=5000)
            return
    except Exception:
        pass
    # Fallback: dropdown customizado — clica no campo "Agrupamento" pra abrir a
    # lista e depois clica na opção "Vendedor/Produto" pelo texto.
    page.get_by_text("Agrupamento", exact=False).first.click()
    page.wait_for_timeout(300)
    page.get_by_text(valor, exact=True).first.click()


def _preencher_periodo_por_label(page, rotulo_ini, rotulo_fim, data_str):
    """Preenche Data Inicial / Data Final pelo rótulo; se não achar por label, cai
    pro fallback de pegar os dois primeiros campos de data/texto da página."""
    ok_ini = _preencher_por_label_ou_placeholder(page, [rotulo_ini], data_str)
    ok_fim = _preencher_por_label_ou_placeholder(page, [rotulo_fim], data_str)
    if ok_ini and ok_fim:
        return
    campos = page.locator("input[type='text'], input[type='date']").all()
    if len(campos) >= 2:
        campos[0].fill(data_str)
        campos[1].fill(data_str)


def _capturar_pdf_via_clique(page, context, botao):
    """Clica no botão que gera o relatório (PDF/Imprimir) e captura os bytes do PDF
    resultante, tentando 3 estratégias em ordem (o comportamento exato não pôde ser
    verificado ao vivo, então cobrimos os 3 jeitos mais comuns desse tipo de sistema
    legado):
      A) o clique dispara um download de verdade;
      B) o clique abre uma NOVA ABA com o PDF (visualizador do Chrome ou uma URL
         que responde application/pdf);
      C) a resposta do clique é interceptada como resposta de rede com
         content-type PDF, sem abrir aba nova nem virar "download"."""
    try:
        with page.expect_download(timeout=10000) as download_info:
            botao.click()
        caminho_tmp = download_info.value.path()
        with open(caminho_tmp, "rb") as f:
            return f.read()
    except PlaywrightTimeoutError:
        pass

    try:
        with context.expect_page(timeout=10000) as nova_pagina_info:
            botao.click()
        nova_pagina = nova_pagina_info.value
        nova_pagina.wait_for_load_state("domcontentloaded", timeout=30000)
        resposta = nova_pagina.request.get(nova_pagina.url) if hasattr(nova_pagina, "request") else None
        if resposta is not None:
            return resposta.body()
        return nova_pagina.pdf()
    except PlaywrightTimeoutError:
        pass

    with page.expect_response(
        lambda r: "pdf" in (r.headers.get("content-type", "").lower()) or r.url.lower().endswith(".pdf"),
        timeout=15000,
    ) as resposta_info:
        botao.click()
    return resposta_info.value.body()


def logar_e_baixar_relatorios(playwright, loja, url_login, login, senha, empresa_texto, headed=False):
    """Abre o navegador, loga no SGI e captura, na MESMA sessão, os dois relatórios
    do dia: 'Totais de Vendas' e 'Totais de Vendas Por Produto'. Retorna
    {"vendas": bytes, "produtos": bytes}."""
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

        hoje_str = date.today().strftime("%d/%m/%Y")

        # ---- 2) Relatório 1: Vendas > Totais de Vendas ----
        page.get_by_text("Vendas", exact=True).first.click()
        page.wait_for_timeout(500)
        page.get_by_text("Totais de Vendas", exact=True).first.click()
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _salvar_debug(page, loja, "form_vendas_antes_empresa")

        # Campo "Empresa" DENTRO do formulário do relatório — dropdown separado do
        # da tela de login, o valor não acompanha automaticamente.
        selecionar_empresa_no_formulario(page, empresa_texto)
        _salvar_debug(page, loja, "form_vendas_depois_empresa")

        campos_periodo = page.locator("text=Período").locator("xpath=following::input").all()
        if len(campos_periodo) >= 2:
            campos_periodo[0].fill(hoje_str)
            campos_periodo[1].fill(hoje_str)
        else:
            print(f"  [{loja}] aviso: não achei os 2 campos de período (Totais de Vendas) — o "
                  "relatório pode sair com a data padrão do formulário em vez de hoje.")

        botao_pdf_vendas = page.get_by_role("button", name="PDF", exact=False)
        pdf_vendas = _capturar_pdf_via_clique(page, context, botao_pdf_vendas)

        # ---- 3) Relatório 2: Vendas > Totais de Vendas Por Produto ----
        # Reabre o menu "Vendas" (pode ter fechado ao navegar) e clica no item do
        # segundo relatório. Nome exato confirmado pelo roteiro em PDF do usuário.
        try:
            page.get_by_text("Vendas", exact=True).first.click()
            page.wait_for_timeout(500)
        except Exception:
            pass
        page.get_by_text("Totais de Vendas Por Produto", exact=True).first.click()
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _salvar_debug(page, loja, "form_produto_antes_preencher")

        _preencher_periodo_por_label(page, "Data Inicial", "Data Final", hoje_str)

        # Agrupamento SEMPRE "Vendedor/Produto" — exigência do usuário, é o formato
        # que o parser espera (uma seção por vendedor).
        selecionar_agrupamento_vendedor_produto(page)
        _salvar_debug(page, loja, "form_produto_depois_preencher")

        botao_imprimir = page.get_by_role("button", name="Imprimir", exact=False)
        pdf_produtos = _capturar_pdf_via_clique(page, context, botao_imprimir)

        return {"vendas": pdf_vendas, "produtos": pdf_produtos}

    except Exception:
        _salvar_debug(page, loja, "erro")
        raise
    finally:
        browser.close()


def _sincronizar_vendas_diarias(loja, hoje, pdf_bytes):
    resultado = parse_relatorio_vendas_pdf(pdf_bytes)
    if not resultado["linhas"]:
        with open(f"debug_{loja.replace(' ', '_')}_vendas_pdf_vazio.pdf", "wb") as f:
            f.write(pdf_bytes)
        print(f"[{loja}] Nenhuma linha de vendedor reconhecida em 'Totais de Vendas' "
              f"(salvo em debug_{loja.replace(' ', '_')}_vendas_pdf_vazio.pdf para inspeção).")
        return

    vendedores_df = db.get_vendedores(loja=loja, apenas_ativos=True)
    matched, nao_encontrados = casar_vendedores(resultado["linhas"], vendedores_df)
    if nao_encontrados:
        print(f"[{loja}] Ignorados em 'Totais de Vendas' (sem correspondência no cadastro): "
              + ", ".join(nao_encontrados))

    for item in matched:
        db.upsert_venda(item["vendedor_id"], hoje, float(item["t_liq"]), int(item["n_ped"]))
        print(f"[{loja}] {item['nome']}: {item['n_ped']} pedido(s), R$ {item['t_liq']:.2f}")

    print(f"[{loja}] {len(matched)} vendedor(es) sincronizado(s) em 'Totais de Vendas' "
          f"para {hoje.strftime('%d/%m/%Y')}.")


def _sincronizar_vendas_produtos(loja, hoje, pdf_bytes):
    resultado = parse_relatorio_produtos_pdf(pdf_bytes)
    if not resultado["produtos"]:
        with open(f"debug_{loja.replace(' ', '_')}_produtos_pdf_vazio.pdf", "wb") as f:
            f.write(pdf_bytes)
        print(f"[{loja}] Nenhum produto reconhecido em 'Totais de Vendas Por Produto' "
              f"(salvo em debug_{loja.replace(' ', '_')}_produtos_pdf_vazio.pdf para inspeção).")
        return

    vendedores_df = db.get_vendedores(loja=loja, apenas_ativos=True)
    matched, nao_encontrados = casar_produtos_vendedores(resultado["produtos"], vendedores_df)
    if nao_encontrados:
        print(f"[{loja}] Ignorados em 'Totais de Vendas Por Produto' (sem correspondência no cadastro): "
              + ", ".join(nao_encontrados))

    por_vendedor = {}
    for item in matched:
        por_vendedor.setdefault(item["vendedor_id"], []).append(item)

    for vendedor_id, produtos in por_vendedor.items():
        db.upsert_vendas_produtos_dia(vendedor_id, hoje, produtos)

    total_valor = sum(p["valor_total"] or 0.0 for p in matched)
    print(f"[{loja}] {len(matched)} produto(s) de {len(por_vendedor)} vendedor(es) sincronizado(s) em "
          f"'Totais de Vendas Por Produto' para {hoje.strftime('%d/%m/%Y')} — R$ {total_valor:,.2f}.")


def sincronizar_loja(playwright, loja, hoje, headed=False):
    url_login = os.environ["SGI_URL_LOGIN"]
    login = os.environ["SGI_LOGIN"]
    senha = os.environ["SGI_SENHA"]
    empresa_texto = os.environ[LOJA_PARA_EMPRESA_ENV[loja]]

    print(f"[{loja}] Fazendo login e gerando relatórios de {hoje.strftime('%d/%m/%Y')}...")
    pdfs = logar_e_baixar_relatorios(playwright, loja, url_login, login, senha, empresa_texto, headed=headed)

    _sincronizar_vendas_diarias(loja, hoje, pdfs["vendas"])
    _sincronizar_vendas_produtos(loja, hoje, pdfs["produtos"])


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