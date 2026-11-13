"""
scripts/sync_sgi.py - Sincroniza as vendas do dia a partir do SGI Solution direto
no Postgres do painel, puxando DOIS relatórios na mesma sessão logada:
  1) "Totais de Vendas" -> Nº Ped e T. Liq. por vendedor (vendas_diarias).
  2) "Totais de Vendas Por Produto" -> produtos vendidos por vendedor, com
     marca/fornecedor (vendas_produtos_diarias) — agrupamento sempre
     "Vendedor/Produto".

Roteiro de cada um: Login -> Vendas -> <relatório> -> período = dia -> gerar
PDF -> extrair dados.

REGRA DE NEGÓCIO: o "Realizado" (vendas_diarias.valor_realizado) é o T. LIQ.
(líquido: bruto - bonificação/desconto - troca) do relatório "Totais de Vendas",
e Pedidos = Nº Ped desse mesmo relatório. O relatório por produto alimenta SÓ o
detalhe por produto/marca (vendas_produtos_diarias): a coluna "Valor Total" dele
não é o líquido, então nunca deve ser gravada em vendas_diarias.

Pensado para rodar via GitHub Actions (agendado às 12h e 19h de Brasília, e sob
demanda pelo botão "Sincronizar com o SGI agora" do painel, que dispara esse mesmo
workflow via API), mas roda local também para testar/depurar.

As funções de login/navegação/captura de PDF daqui são reaproveitadas por
scripts/backfill_produtos.py (importação histórica de Vendas por Produto) — evite
duplicar essa lógica lá, só chame as funções deste módulo.

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
import base64
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

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
    normalizar_nome_match,
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
                    # Tira o foco do campo depois de preencher — alguns componentes de
                    # data em JS só "confirmam" o valor digitado num evento de blur,
                    # não no simples fill() do DOM.
                    try:
                        campo.first.press("Tab")
                    except Exception:
                        pass
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


def _preencher_data_inicial_final(page, data_ini_str, data_fim_str, rotulo_ini="Data Inicial", rotulo_fim="Data Final"):
    """Preenche os campos de período (Data Inicial / Data Final) com datas
    possivelmente diferentes — usado tanto pro dia de hoje (sync diário) quanto
    por um mês/dia específico (backfill histórico, reprocessamento). Ordem de
    estratégias, da mais confiável pra mais arriscada:
      1) input[name='data_inicial']/[name='data_final'] — confirmado via HTML
         real capturado em debug que é esse o campo verdadeiro no formulário
         'Totais de Vendas' (um widget bootstrap-datepicker/input-daterange).
         Como os dois relatórios do SGI parecem seguir o mesmo template, tenta
         aqui primeiro pros dois.
      2) Pelo rótulo acessível (rotulo_ini/rotulo_fim) — só funciona se o
         <label> do formulário tiver um `for` que bata com o `id` do input; no
         formulário 'Totais de Vendas' isso NÃO acontece (for≠id), por isso a
         estratégia 1 existe.
      3) Fallback genérico (2 primeiros input[type=text/date] da página) — a
         mais arriscada: formulários com dropdowns bootstrap-select têm uma
         caixa de busca escondida (<input aria-label="Search">) POR dropdown,
         então esse fallback pode acabar preenchendo o campo errado. Só usa se
         as duas estratégias anteriores falharem."""
    campo_ini = page.locator("input[name='data_inicial']")
    campo_fim = page.locator("input[name='data_final']")
    if campo_ini.count() > 0 and campo_fim.count() > 0:
        # O campo é um bootstrap-datepicker (jQuery) — .fill() seta o value via DOM
        # e não necessariamente dispara os eventos de teclado que esse tipo de
        # plugin escuta pra atualizar o estado INTERNO dele. Confirmado com o
        # usuário testando manualmente no site: clicar abre um calendário, dá pra
        # digitar no padrão DD/MM/AAAA, e é o TAB que confirma/muda de campo (uma
        # tentativa anterior usando Escape pra fechar o calendário na verdade
        # CANCELAVA a digitação, por isso o valor nunca mudava). Por isso aqui:
        # clica, seleciona tudo, digita de verdade caractere a caractere (dispara
        # os eventos de teclado que o plugin escuta) e confirma com Tab.
        for campo, valor in ((campo_ini.first, data_ini_str), (campo_fim.first, data_fim_str)):
            campo.click()
            campo.press("Control+A")
            campo.press_sequentially(valor, delay=30)
            campo.press("Tab")

        # CONFERE o que ficou nos campos (antes assumia que a digitação tinha pegado).
        # Num range de datas encadeado (início/fim), o plugin pode reajustar o campo
        # final ao mexer no inicial, e o relatório sairia de outro período sem
        # ninguém perceber. Se não bater, tenta setar direto pelo plugin/eventos.
        def _valores_atuais():
            return campo_ini.first.input_value().strip(), campo_fim.first.input_value().strip()

        if _valores_atuais() != (data_ini_str, data_fim_str):
            page.evaluate(
                """([vi, vf]) => {
                    const setar = (nome, v) => {
                        const el = document.querySelector(`input[name='${nome}']`);
                        if (!el) return;
                        if (window.jQuery && window.jQuery.fn && window.jQuery.fn.datepicker) {
                            try { window.jQuery(el).datepicker('update', v); } catch (e) {}
                        }
                        el.value = v;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    };
                    setar('data_inicial', vi);
                    setar('data_final', vf);
                }""",
                [data_ini_str, data_fim_str],
            )
            page.wait_for_timeout(300)
        atuais = _valores_atuais()
        if atuais != (data_ini_str, data_fim_str):
            print(f"  [aviso] campos de período ficaram {atuais} em vez de "
                  f"{(data_ini_str, data_fim_str)}.")
            return False
        return True

    ok_ini = _preencher_por_label_ou_placeholder(page, [rotulo_ini], data_ini_str)
    ok_fim = _preencher_por_label_ou_placeholder(page, [rotulo_fim], data_fim_str)
    if ok_ini and ok_fim:
        return True

    print(
        "  [aviso] usando fallback genérico pra preencher o período — pode pegar o campo "
        "errado se o formulário tiver dropdowns com caixa de busca escondida."
    )
    campos = page.locator("input[type='text'], input[type='date']").all()
    if len(campos) >= 2:
        campos[0].fill(data_ini_str)
        campos[1].fill(data_fim_str)
        return True
    return False


def _pdf_por_download(page, context, botao, timeout_ms):
    with page.expect_download(timeout=timeout_ms) as download_info:
        botao.click()
    with open(download_info.value.path(), "rb") as f:
        return f.read()


def _pdf_por_nova_aba(page, context, botao, timeout_ms):
    with context.expect_page(timeout=timeout_ms) as nova_pagina_info:
        botao.click()
    nova_pagina = nova_pagina_info.value
    try:
        nova_pagina.wait_for_load_state("domcontentloaded", timeout=30000)
        resposta = nova_pagina.request.get(nova_pagina.url) if hasattr(nova_pagina, "request") else None
        return resposta.body() if resposta is not None else nova_pagina.pdf()
    finally:
        # Fecha a aba nova — no backfill dezenas de relatórios saem em sequência na
        # mesma sessão; sem fechar, as abas se acumulam.
        try:
            nova_pagina.close()
        except Exception:
            pass


def _pdf_por_resposta(page, context, botao, timeout_ms):
    with page.expect_response(
        lambda r: "pdf" in (r.headers.get("content-type", "").lower()) or r.url.lower().endswith(".pdf"),
        timeout=max(timeout_ms, 15000),
    ) as resposta_info:
        botao.click()
    return resposta_info.value.body()


_ESTRATEGIAS_PDF = {
    "download": _pdf_por_download,
    "nova_aba": _pdf_por_nova_aba,
    "resposta": _pdf_por_resposta,
}
# Lembra qual estratégia funcionou na última captura. Antes cada relatório tentava
# A (download, 10s de espera) -> B -> C do zero; se o SGI abre uma aba nova (B), TODO
# relatório gastava 10s esperando um download que nunca vinha — e ainda clicava duas
# vezes (deixando abas sobrando). Num mês inteiro, dia a dia, isso eram minutos
# jogados fora. Agora a estratégia que deu certo vira a primeira tentativa.
_ESTRATEGIA_PDF_OK = {"nome": None}


def _fechar_abas_extras(context, manter):
    """Fecha abas que sobraram de um clique que não era a estratégia certa. `manter`:
    abas que devem continuar abertas (a principal e a de 'Totais de Vendas')."""
    for p in list(context.pages):
        if p not in manter:
            try:
                p.close()
            except Exception:
                pass


def _capturar_pdf_via_clique(page, context, botao):
    """Clica no botão que gera o relatório (PDF/Imprimir) e captura os bytes do PDF
    resultante. O comportamento exato do SGI não pôde ser verificado ao vivo, então
    cobre os 3 jeitos mais comuns desse tipo de sistema legado:
      download) o clique dispara um download de verdade;
      nova_aba) o clique abre uma NOVA ABA com o PDF;
      resposta) a resposta do clique é interceptada como resposta de rede com
         content-type PDF, sem abrir aba nova nem virar "download".
    A estratégia que funcionou da última vez é tentada primeiro."""
    ordem = list(_ESTRATEGIAS_PDF)
    conhecida = _ESTRATEGIA_PDF_OK["nome"]
    if conhecida in ordem:
        ordem.remove(conhecida)
        ordem.insert(0, conhecida)

    abas_antes = list(context.pages)
    ultimo_erro = None
    for nome in ordem:
        # Estratégia já comprovada: espera folgada. Ainda sem comprovar: só 10s pra
        # descobrir logo se é essa ou a próxima.
        timeout_ms = 30000 if nome == conhecida else 10000
        try:
            conteudo = _ESTRATEGIAS_PDF[nome](page, context, botao, timeout_ms)
            _ESTRATEGIA_PDF_OK["nome"] = nome
            return conteudo
        except PlaywrightTimeoutError as e:
            ultimo_erro = e
            _fechar_abas_extras(context, abas_antes)
    raise ultimo_erro


# --------------------------------------------------------------------------
# Funções reutilizáveis de login/navegação (usadas pelo sync diário e pelo
# backfill histórico de scripts/backfill_produtos.py).
# --------------------------------------------------------------------------
def fazer_login(playwright, loja, url_login, login, senha, empresa_texto, headed=False):
    """Abre o navegador e loga no SGI. Retorna (browser, context, page) já logados
    — quem chama é responsável por navegar aos relatórios e por fechar o browser
    (browser.close()) no final."""
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

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

    return browser, context, page


def navegar_ate_totais_de_vendas(page):
    page.get_by_text("Vendas", exact=True).first.click()
    page.wait_for_timeout(500)
    page.get_by_text("Totais de Vendas", exact=True).first.click()
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)


def navegar_ate_totais_de_vendas_por_produto(page):
    # Reabre o menu "Vendas" (pode ter fechado ao navegar antes) — não é erro se já
    # estiver visível, por isso o try/except.
    try:
        page.get_by_text("Vendas", exact=True).first.click()
        page.wait_for_timeout(500)
    except Exception:
        pass
    page.get_by_text("Totais de Vendas Por Produto", exact=True).first.click()
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)


def _baixar_pdf_vendas_via_post(page, data_ini_str, data_fim_str):
    """Gera o PDF de 'Totais de Vendas' SEM passar pelo widget bootstrap-datepicker
    (que se mostrou não confiável de automatizar): monta o FormData do próprio
    formulário (#formRelatorio, já com Empresa e demais filtros nos valores padrão
    da tela), sobrescreve data_inicial/data_final e faz POST para a rota de PDF do
    formulário (atributo rota-pdf) usando a sessão logada da página. Retorna os
    bytes do PDF ou None se algo não bater (aí o chamador cai no fluxo por clique)."""
    try:
        resultado = page.evaluate(
            """async ({d1, d2}) => {
                const form = document.querySelector('#formRelatorio');
                if (!form) return {erro: 'formulario #formRelatorio nao encontrado'};
                const url = form.getAttribute('rota-pdf');
                if (!url) return {erro: 'atributo rota-pdf ausente'};
                const fd = new FormData(form);
                fd.set('data_inicial', d1);
                fd.set('data_final', d2);
                const r = await fetch(url, {method: 'POST', body: fd, credentials: 'same-origin'});
                const buf = new Uint8Array(await r.arrayBuffer());
                let bin = '';
                for (let i = 0; i < buf.length; i += 0x8000) {
                    bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
                }
                return {status: r.status, ctype: r.headers.get('content-type') || '', b64: btoa(bin)};
            }""",
            {"d1": data_ini_str, "d2": data_fim_str},
        )
    except Exception as e:
        print(f"  [aviso] POST direto do PDF falhou ({e}); usando o fluxo por clique.")
        return None

    if resultado.get("erro"):
        print(f"  [aviso] POST direto indisponível: {resultado['erro']}; usando o fluxo por clique.")
        return None
    conteudo = base64.b64decode(resultado.get("b64", ""))
    if resultado.get("status") != 200 or not conteudo.startswith(b"%PDF"):
        print(
            f"  [aviso] POST direto não devolveu PDF (HTTP {resultado.get('status')}, "
            f"{resultado.get('ctype')}); usando o fluxo por clique."
        )
        return None
    return conteudo


def _validar_periodo_pdf_vendas(pdf_bytes, data_ini_str, data_fim_str):
    """Garante que o PDF de 'Totais de Vendas' é do período pedido. Um relatório de
    outro período (o bug antigo: saía sempre a data de hoje) NUNCA pode ser gravado
    como se fosse do dia pedido — então divergência aborta com erro."""
    resultado = parse_relatorio_vendas_pdf(pdf_bytes)
    ini, fim = resultado.get("data_ini"), resultado.get("data_fim")
    if ini is None or fim is None:
        print("  [aviso] não consegui ler o período impresso no PDF de 'Totais de Vendas'; "
              "não foi possível validar a data.")
        return
    esperado_ini = datetime.strptime(data_ini_str, "%d/%m/%Y").date()
    esperado_fim = datetime.strptime(data_fim_str, "%d/%m/%Y").date()
    if (ini, fim) != (esperado_ini, esperado_fim):
        raise RuntimeError(
            f"PDF de 'Totais de Vendas' veio com período {ini:%d/%m/%Y} a {fim:%d/%m/%Y}, "
            f"mas o pedido era {data_ini_str} a {data_fim_str} — nada foi gravado."
        )


def gerar_pdf_totais_de_vendas(page, context, empresa_texto, data_ini_str, data_fim_str, loja=None):
    """Assume que já está na página 'Totais de Vendas' (ver navegar_ate_totais_de_vendas).
    Reseleciona a Empresa (o valor não acompanha automaticamente o do login) e o
    período pedido, clica em PDF e retorna os bytes.

    IMPORTANTE (bug em investigação, confirmado via HTML/PDF reais capturados em
    debug): os campos de período desse formulário são um widget bootstrap-datepicker
    (input-daterange) com <input name="data_inicial"> e <input name="data_final">
    — o <label for="data_inicial"> desse form NÃO tem um id correspondente no
    input (só o atributo name), então page.get_by_label() nunca encontrava o
    campo. Mesmo apontando direto pro seletor certo, o relatório continuou saindo
    com a data de hoje — sinal de que .fill() não é suficiente pra esse widget
    (plugins jQuery de data costumam só atualizar o estado interno deles com
    eventos de teclado de verdade, não com o value setado direto via DOM). Por
    isso agora simula digitação real (click + selecionar tudo + digitar
    caractere a caractere) e fecha o calendário com Escape."""
    selecionar_empresa_no_formulario(page, empresa_texto)

    # Caminho principal: POST direto com as datas (ignora o datepicker). Só cai no
    # fluxo por clique abaixo se o POST não devolver um PDF válido.
    pdf = _baixar_pdf_vendas_via_post(page, data_ini_str, data_fim_str)
    if pdf is not None:
        _validar_periodo_pdf_vendas(pdf, data_ini_str, data_fim_str)
        return pdf

    preenchido = _preencher_data_inicial_final(page, data_ini_str, data_fim_str, "Data Inicial", "Data Final")
    if not preenchido:
        print(
            f"  [aviso] não consegui preencher o período (pedido: {data_ini_str} a {data_fim_str}) "
            "no formulário 'Totais de Vendas' — o relatório provavelmente vai sair com a data "
            "padrão do formulário (hoje), não a pedida."
        )
    if loja:
        _salvar_debug(page, loja, "form_vendas_depois_periodo")

    botao_pdf = page.get_by_role("button", name="PDF", exact=False)
    pdf = _capturar_pdf_via_clique(page, context, botao_pdf)
    _validar_periodo_pdf_vendas(pdf, data_ini_str, data_fim_str)
    return pdf


def gerar_pdf_totais_de_vendas_por_produto(page, context, data_ini_str, data_fim_str):
    """Assume que já está na página 'Totais de Vendas Por Produto' (ver
    navegar_ate_totais_de_vendas_por_produto). Preenche o período pedido, garante
    Agrupamento = 'Vendedor/Produto', clica em Imprimir e retorna os bytes."""
    _preencher_data_inicial_final(page, data_ini_str, data_fim_str, "Data Inicial", "Data Final")
    selecionar_agrupamento_vendedor_produto(page)
    botao_imprimir = page.get_by_role("button", name="Imprimir", exact=False)
    return _capturar_pdf_via_clique(page, context, botao_imprimir)


class PeriodoDivergente(Exception):
    """O SGI gerou um relatório de um período diferente do pedido."""


def gerar_pdf_produtos_validado(page, context, data_ini_str, data_fim_str, tentativas=3, loja=None):
    """Como gerar_pdf_totais_de_vendas_por_produto, mas CONFERE o período impresso no
    cabeçalho do PDF contra o pedido (o 'Totais de Vendas' já era validado; este não)
    e tenta de novo, recarregando o formulário, se divergir. Se continuar divergindo,
    levanta PeriodoDivergente — NUNCA devolve um relatório de outro período, que seria
    gravado como se fosse do dia pedido.

    Retorna (pdf_bytes, periodo_confirmado). periodo_confirmado=False quando o PDF
    não traz o cabeçalho de período (não dá pra validar)."""
    esperado = (datetime.strptime(data_ini_str, "%d/%m/%Y").date(), datetime.strptime(data_fim_str, "%d/%m/%Y").date())
    ultimo = None
    for tentativa in range(1, tentativas + 1):
        pdf_bytes = gerar_pdf_totais_de_vendas_por_produto(page, context, data_ini_str, data_fim_str)
        r = parse_relatorio_produtos_pdf(pdf_bytes)
        ini, fim = r.get("data_ini"), r.get("data_fim")
        if ini is None or fim is None:
            print("  [aviso] PDF 'Por Produto' sem cabeçalho de período — não foi possível validar o período.")
            return pdf_bytes, False
        if (ini, fim) == esperado:
            return pdf_bytes, True

        ultimo = f"'Por Produto' pedido {data_ini_str} a {data_fim_str}, veio {ini:%d/%m/%Y} a {fim:%d/%m/%Y}"
        print(f"  [aviso] período divergente (tentativa {tentativa}/{tentativas}): {ultimo}")
        if loja:
            _salvar_debug(page, loja, f"periodo_divergente_{tentativa}")
        if tentativa < tentativas:
            try:
                navegar_ate_totais_de_vendas_por_produto(page)  # recarrega o formulário limpo
            except Exception:
                pass
            page.wait_for_timeout(1000 * tentativa)
    raise PeriodoDivergente(ultimo)


def abrir_aba_totais_de_vendas(page, context):
    """Abre uma SEGUNDA aba (mesma sessão logada) já no formulário 'Totais de
    Vendas', pra alternar entre os dois relatórios sem renavegar pelos menus a cada
    dia. `page` é a aba principal, já logada."""
    aba = context.new_page()
    aba.goto(page.url, wait_until="domcontentloaded", timeout=60000)
    aba.wait_for_timeout(1500)
    navegar_ate_totais_de_vendas(aba)
    return aba


def _acumulado_por_nome(pdf_bytes):
    """{nome normalizado: (t_liq, n_ped)} de um PDF 'Totais de Vendas'."""
    if not pdf_bytes:
        return {}
    r = parse_relatorio_vendas_pdf(pdf_bytes)
    return {normalizar_nome_match(l["nome_pdf"]): (l["t_liq"], l["n_ped"]) for l in r["linhas"]}


def _totais_produtos_por_nome(pdf_bytes):
    """{nome normalizado: soma do Valor Total} do PDF 'Por Produto' (referência
    independente para validar o líquido do dia)."""
    if not pdf_bytes:
        return {}
    tot = {}
    for p in parse_relatorio_produtos_pdf(pdf_bytes)["produtos"]:
        nome = normalizar_nome_match(p.get("vendedor_nome_pdf") or "")
        tot[nome] = tot.get(nome, 0.0) + float(p.get("valor_total") or 0.0)
    return tot


def _valores_do_dia(linhas, pdf_anterior, ref_produtos):
    """Devolve {nome normalizado: (valor, pedidos, origem)} do dia.

    O SGI pode devolver o 'Totais de Vendas' de um dia como ACUMULADO (histórico
    até aquele dia) em vez do valor do dia — foi o que inflou Gleicia/Jadila/Luan.
    Por isso: (1) compara 'valor bruto do PDF' e 'PDF do dia - PDF do dia anterior'
    com o total do relatório por produto do mesmo dia e usa o modo que bate; (2) se
    nenhum bate, grava o total por produto (e avisa), nunca um valor inflado."""
    prev = _acumulado_por_nome(pdf_anterior)
    ref = ref_produtos or {}
    chave = lambda l: normalizar_nome_match(l["nome_pdf"])
    comparaveis = [l for l in linhas if ref.get(chave(l), 0) > 200]
    modo = "bruto"
    if prev and comparaveis:
        err_bruto = sum(abs(l["t_liq"] - ref[chave(l)]) for l in comparaveis)
        err_dif = sum(abs(l["t_liq"] - prev.get(chave(l), (0.0, 0))[0] - ref[chave(l)]) for l in comparaveis)
        if err_dif < err_bruto:
            modo = "diferenca"
    out = {}
    for l in linhas:
        k = chave(l)
        v, q = l["t_liq"], l["n_ped"]
        if modo == "diferenca":
            pv, pq = prev.get(k, (0.0, 0))
            v, q = v - pv, q - pq
        r = ref.get(k)
        origem = modo
        if r is not None and abs(v - r) > max(50.0, 0.25 * r):
            print(f"  [aviso] {l['nome_pdf']}: valor do relatório (R$ {v:,.2f}, modo {modo}) não bate com o "
                  f"total por produto (R$ {r:,.2f}); gravando o total por produto.")
            v, origem = r, "produto"
            q = max(q, 1) if q > 0 else q
        elif r is None and modo == "bruto" and prev:
            pv = prev.get(k, (0.0, 0))[0]
            if v > 0 and abs(v - pv) < 0.005:  # sem vendas no dia: repete o acumulado
                continue
        if v < 0 or q < 0:
            continue
        out[k] = (v, q, origem)
    return out, modo


def _sincronizar_vendas_diarias(loja, dia, pdf_bytes, pdf_anterior=None, ref_produtos=None, limpar_ausentes=False):
    """Grava em vendas_diarias o T. LIQ. (líquido) e o Nº Ped REAIS de cada
    vendedor, vindos do relatório 'Totais de Vendas'. É a fonte oficial do
    Realizado/Pedidos do painel. Valida o valor contra o relatório por produto do
    mesmo dia e converte relatório acumulado em valor do dia (ver _valores_do_dia)."""
    resultado = parse_relatorio_vendas_pdf(pdf_bytes)
    if not resultado["linhas"]:
        with open(f"debug_{loja.replace(' ', '_')}_vendas_pdf_vazio.pdf", "wb") as f:
            f.write(pdf_bytes)
        print(f"[{loja}] {dia:%d/%m/%Y}: nenhuma linha de vendedor em 'Totais de Vendas' "
              "(dia sem vendas ou PDF diferente do esperado; PDF salvo para inspeção). "
              "Nada gravado em vendas_diarias.")
        return 0

    vendedores_df = db.get_vendedores(loja=loja, apenas_ativos=True)
    matched, nao_encontrados = casar_vendedores(resultado["linhas"], vendedores_df)
    if nao_encontrados:
        print(f"[{loja}] Ignorados em 'Totais de Vendas' (sem correspondência no cadastro): "
              + ", ".join(nao_encontrados))

    valores, modo = _valores_do_dia(resultado["linhas"], pdf_anterior, ref_produtos)
    total, gravados, ids_gravados = 0.0, 0, []
    for item in matched:
        k = normalizar_nome_match(item["nome"])
        if k not in valores:
            continue
        v, q, _origem = valores[k]
        db.upsert_venda(item["vendedor_id"], dia, float(v), int(q))
        ids_gravados.append(item["vendedor_id"])
        total += v
        gravados += 1

    if limpar_ausentes:
        # Reprocessamento de período: vendedor sem venda no dia não é sobrescrito pelo
        # upsert, então um valor errado (ex.: acumulado) de uma sincronização anterior
        # ficaria no painel. O SGI é a fonte oficial: o que ele não traz no dia sai.
        apagadas = db.limpar_vendas_dia_loja(loja, dia, ids_gravados, tabelas=("vendas_diarias",))
        if apagadas:
            print(f"[{loja}] {dia:%d/%m/%Y}: {apagadas} lançamento(s) antigo(s) de vendedor(es) sem "
                  "venda no dia removido(s) de vendas_diarias.")

    print(f"[{loja}] {dia:%d/%m/%Y}: {gravados} vendedor(es) — líquido R$ {total:,.2f} "
          f"(modo {modo}; vendas_diarias atualizado a partir de 'Totais de Vendas').")
    return gravados


def _sincronizar_vendas_produtos(loja, dia, pdf_bytes, limpar_ausentes=False):
    """Faz o parse de 'Totais de Vendas Por Produto' e grava SÓ o detalhe por
    produto/marca (vendas_produtos_diarias). NÃO toca em vendas_diarias: o 'Valor
    Total' desse relatório não é o líquido — o Realizado vem de
    _sincronizar_vendas_diarias."""
    resultado = parse_relatorio_produtos_pdf(pdf_bytes)
    if not resultado["produtos"]:
        with open(f"debug_{loja.replace(' ', '_')}_produtos_pdf_vazio.pdf", "wb") as f:
            f.write(pdf_bytes)
        print(f"[{loja}] {dia:%d/%m/%Y}: nenhum produto reconhecido em 'Totais de Vendas Por "
              "Produto' (PDF salvo para inspeção).")
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
        db.upsert_vendas_produtos_dia(vendedor_id, dia, produtos)
    if limpar_ausentes:
        db.limpar_vendas_dia_loja(loja, dia, list(por_vendedor), tabelas=("vendas_produtos_diarias",))

    print(f"[{loja}] {dia:%d/%m/%Y}: {len(matched)} produto(s) de {len(por_vendedor)} vendedor(es) "
          "gravados em vendas_produtos_diarias.")


def sincronizar_loja(playwright, loja, datas, headed=False):
    """Loga UMA vez e sincroniza cada data de `datas` (lista de date): primeiro o
    líquido/pedidos ('Totais de Vendas'), depois o detalhe por produto. Uma falha
    num relatório/dia não impede os demais, mas faz a execução terminar com erro
    (o workflow fica vermelho em vez de passar em branco)."""
    url_login = os.environ["SGI_URL_LOGIN"]
    login = os.environ["SGI_LOGIN"]
    senha = os.environ["SGI_SENHA"]
    empresa_texto = os.environ[LOJA_PARA_EMPRESA_ENV[loja]]

    print(f"[{loja}] Fazendo login...")
    browser, context, page = fazer_login(playwright, loja, url_login, login, senha, empresa_texto, headed=headed)
    erros = []
    try:
        aba_vendas = abrir_aba_totais_de_vendas(page, context)
        navegar_ate_totais_de_vendas_por_produto(page)

        cache_pdf = {}
        reprocessando = len(datas) > 1  # --data-fim: SGI é a fonte da verdade, limpa o que ele não traz

        def _pdf_vendas(d):
            if d not in cache_pdf:
                cache_pdf[d] = gerar_pdf_totais_de_vendas(
                    aba_vendas, context, empresa_texto, d.strftime("%d/%m/%Y"), d.strftime("%d/%m/%Y"), loja=loja
                )
            return cache_pdf[d]

        for dia in datas:
            data_str = dia.strftime("%d/%m/%Y")
            sufixo = dia.strftime("%Y%m%d")
            print(f"[{loja}] Sincronizando {data_str}...")

            pdf_vendas = pdf_anterior = pdf_produtos = None
            try:
                pdf_vendas = _pdf_vendas(dia)
                with open(f"debug_{loja.replace(' ', '_')}_vendas_{sufixo}.pdf", "wb") as f:
                    f.write(pdf_vendas)
            except Exception as e:
                _salvar_debug(aba_vendas, loja, f"erro_vendas_{sufixo}")
                erros.append(f"{loja} {data_str} (Totais de Vendas): {e}")
                print(f"[{loja}] ERRO em 'Totais de Vendas' {data_str}: {e}")
            try:
                pdf_anterior = _pdf_vendas(dia - timedelta(days=1))  # base p/ converter acumulado em diário
            except Exception as e:
                print(f"[{loja}] aviso: não obtive o relatório do dia anterior ({e}).")
            try:
                pdf_produtos, _ = gerar_pdf_produtos_validado(page, context, data_str, data_str, loja=loja)
                with open(f"debug_{loja.replace(' ', '_')}_produtos_{sufixo}.pdf", "wb") as f:
                    f.write(pdf_produtos)
            except Exception as e:
                _salvar_debug(page, loja, f"erro_produtos_{sufixo}")
                erros.append(f"{loja} {data_str} (Por Produto): {e}")
                print(f"[{loja}] ERRO em 'Por Produto' {data_str}: {e}")

            if pdf_vendas is not None:
                try:
                    ref = _totais_produtos_por_nome(pdf_produtos) if pdf_produtos else None
                    _sincronizar_vendas_diarias(loja, dia, pdf_vendas, pdf_anterior, ref, limpar_ausentes=reprocessando)
                except Exception as e:
                    erros.append(f"{loja} {data_str} (gravar vendas): {e}")
                    print(f"[{loja}] ERRO ao gravar vendas {data_str}: {e}")
            if pdf_produtos is not None:
                try:
                    _sincronizar_vendas_produtos(loja, dia, pdf_produtos, limpar_ausentes=reprocessando)
                except Exception as e:
                    erros.append(f"{loja} {data_str} (gravar produtos): {e}")
                    print(f"[{loja}] ERRO ao gravar produtos {data_str}: {e}")
            cache_pdf.pop(dia - timedelta(days=1), None)
    finally:
        browser.close()

    if erros:
        raise RuntimeError(" | ".join(erros))


def executar_lojas_em_paralelo(caminho_script, lojas, argv_extra):
    """Roda cada loja num PROCESSO próprio (cada um com seu navegador e sua sessão
    logada no SGI), ao mesmo tempo, em vez de uma depois da outra — a duração total
    cai pra a da loja mais lenta (quase metade com duas lojas). Reaproveitado por
    scripts/backfill_produtos.py. Retorna a lista de lojas cujo processo falhou."""
    procs = []
    for loja in lojas:
        cmd = [sys.executable, "-u", caminho_script, *argv_extra, "--loja", loja]
        procs.append((loja, subprocess.Popen(cmd)))
    return [loja for loja, proc in procs if proc.wait() != 0]


def main():
    parser = argparse.ArgumentParser(description="Sincroniza vendas de um dia a partir do SGI Solution.")
    parser.add_argument(
        "--sequencial", action="store_true",
        help="Roda as lojas uma depois da outra (padrão: em paralelo, um processo por loja).",
    )
    parser.add_argument("--loja", choices=list(LOJA_PARA_EMPRESA_ENV.keys()), help="Rodar só uma loja (debug).")
    parser.add_argument(
        "--data", help="Data específica a sincronizar, formato DD/MM/AAAA (padrão: hoje).",
    )
    parser.add_argument(
        "--data-fim", help="Data final DD/MM/AAAA: sincroniza dia a dia de --data até aqui (inclusive).",
    )
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (só faz sentido local).")
    args = parser.parse_args()

    if args.data:
        try:
            data_alvo = datetime.strptime(args.data, "%d/%m/%Y").date()
        except ValueError:
            print(f"Data inválida: '{args.data}' — use o formato DD/MM/AAAA.")
            sys.exit(1)
    else:
        data_alvo = date.today()

    data_fim = data_alvo
    if args.data_fim:
        try:
            data_fim = datetime.strptime(args.data_fim, "%d/%m/%Y").date()
        except ValueError:
            print(f"Data final inválida: '{args.data_fim}' — use o formato DD/MM/AAAA.")
            sys.exit(1)
        if data_fim < data_alvo:
            print("--data-fim não pode ser anterior a --data.")
            sys.exit(1)
    datas = [data_alvo + timedelta(days=n) for n in range((data_fim - data_alvo).days + 1)]

    lojas = [args.loja] if args.loja else obter_lojas_configuradas()
    if not lojas:
        print(
            "Nenhuma loja configurada — defina SGI_EMPRESA_PORTEIRA e/ou "
            "SGI_EMPRESA_CASA_ADUBO (ou passe --loja)."
        )
        sys.exit(1)

    if len(lojas) > 1 and not args.sequencial and not args.headed:
        argv_extra = [a for a in sys.argv[1:] if a != "--sequencial"]
        falhas = executar_lojas_em_paralelo(os.path.abspath(__file__), lojas, argv_extra)
        if falhas:
            print("\nFalhas nessa execução: " + ", ".join(falhas))
            sys.exit(1)
        print("\nSincronização concluída sem erros.")
        return

    erros = []
    with sync_playwright() as playwright:
        for loja in lojas:
            try:
                sincronizar_loja(playwright, loja, datas, headed=args.headed)
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
