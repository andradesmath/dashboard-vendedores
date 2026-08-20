"""
sgi_relatorio.py - Parsing do PDF 'Relatório de Totais de Vendas' do SGI Solution.

Módulo compartilhado por:
  - dashboard_vendas.py  -> upload manual do PDF (aba Lançamentos Diários)
  - scripts/sync_sgi.py  -> automação (login + geração do relatório do dia via
                             GitHub Actions, 2x ao dia)

Mantido separado para não duplicar a lógica de parsing entre os dois usos — e
para não fazer o script de automação depender do Streamlit (só de `db`).
"""
import io
import re
from datetime import date

from pypdf import PdfReader

LOJAS = ["Porteira", "Casa de Adubo"]


def parse_valor_brl(texto):
    """Converte 'R$ 115.000,00' ou '115000,00' em float. Aceita negativos."""
    texto = texto.strip().replace("R$", "").strip()
    if not texto:
        return None
    negativo = texto.startswith("-")
    texto = texto.lstrip("-").strip()
    texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = float(texto)
    except ValueError:
        return None
    return -valor if negativo else valor


def extrair_texto_pdf(arquivo_bytes):
    """Extrai o texto de todas as páginas de um PDF (bytes) usando pypdf."""
    reader = PdfReader(io.BytesIO(arquivo_bytes))
    return "\n".join((pagina.extract_text() or "") for pagina in reader.pages)


PADRAO_NUM_PDF = re.compile(r"^-?[\d.,]+%?$")
NOME_MAX_TOKENS_PDF = 5


def _eh_token_nome_pdf(token):
    """Um token só conta como parte do nome do vendedor se: não for numérico, não
    tiver ':' nem '.' (evita rótulos de cabeçalho como 'C.', 'T.', 'Nº:') e estiver
    todo em maiúsculas (os nomes no relatório do SGI vêm em CAIXA ALTA; os rótulos de
    coluna do cabeçalho, como 'Vendedor', 'Desc', 'Verba', vêm em Title Case). Um '-'
    isolado também conta — alguns vendedores aparecem como 'NOME - CARGO' (ex.:
    'ALINE - GERENTE'), e precisamos capturar o nome inteiro pra depois normalizar."""
    if token == "-":
        return True
    return (
        not PADRAO_NUM_PDF.match(token) and ":" not in token and "." not in token and token.isupper()
    )


def parse_tokens_relatorio_pdf(tokens):
    """Varre a lista de tokens (palavras) de todo o texto extraído do PDF procurando
    blocos 'NOME (1-4 palavras em caixa alta) + 12 valores numéricos' — o formato de
    cada linha de vendedor do 'Relatório de Totais de Vendas' do SGI (Nº Cli, C. Cli,
    Nº Ped, Nº Item, M. Ped, M. Cli, Bruto, Bonf, Troca, T. Liq., Desc%, Verba).
    Não depende de quebras de linha, então funciona tanto com extratores que mantêm a
    linha inteira quanto com os que colocam um valor por linha."""
    resultados = []
    i, n = 0, len(tokens)
    while i < n:
        if not _eh_token_nome_pdf(tokens[i]):
            i += 1
            continue
        nome_tokens = []
        j = i
        while j < n and len(nome_tokens) < NOME_MAX_TOKENS_PDF and _eh_token_nome_pdf(tokens[j]):
            nome_tokens.append(tokens[j])
            j += 1
        if nome_tokens and j + 12 <= n and all(PADRAO_NUM_PDF.match(v) for v in tokens[j:j + 12]):
            bloco = tokens[j:j + 12]
            nome = " ".join(nome_tokens)
            try:
                n_ped = int(bloco[2])
            except ValueError:
                n_ped = None
            t_liq = parse_valor_brl(bloco[9])
            if n_ped is not None and t_liq is not None:
                resultados.append({"nome_pdf": nome, "n_ped": n_ped, "t_liq": t_liq})
            i = j + 12
        else:
            i += 1
    return resultados


def parse_relatorio_vendas_pdf(arquivo_bytes):
    """Extrai do PDF 'Relatório de Totais de Vendas' do SGI: o período do relatório,
    a loja detectada pelo cabeçalho e a lista de vendedores com Nº Ped e T. Liq."""
    texto = extrair_texto_pdf(arquivo_bytes)

    data_ini = data_fim = None
    m_periodo = re.search(
        r"Per[ií]odo\s+de\s+(\d{2})/(\d{2})/(\d{4})\s+a\s+(\d{2})/(\d{2})/(\d{4})", texto
    )
    if m_periodo:
        d1, m1, a1, d2, m2, a2 = m_periodo.groups()
        try:
            data_ini = date(int(a1), int(m1), int(d1))
            data_fim = date(int(a2), int(m2), int(d2))
        except ValueError:
            pass

    texto_upper = texto.upper()
    loja_detectada = None
    for loja in LOJAS:
        if loja.upper() in texto_upper or loja.upper().replace(" ", "") in texto_upper.replace(" ", ""):
            loja_detectada = loja
            break

    linhas = parse_tokens_relatorio_pdf(texto.split())

    return {"data_ini": data_ini, "data_fim": data_fim, "loja_detectada": loja_detectada, "linhas": linhas}


def normalizar_nome_match(texto):
    """Normaliza um nome para comparação: maiúsculas, sem espaços, sem sufixo de
    cargo (ex.: 'ALINE - GERENTE' vira 'ALINE') — evita falha de match por causa de
    espaçamento diferente entre o PDF e o cadastro, ou de cargo anexado ao nome no
    SGI (inclusive quando a extração do PDF gruda duas palavras do nome sem
    espaço)."""
    texto = texto.strip().upper()
    texto = texto.split(" - ")[0].strip()
    return re.sub(r"\s+", "", texto)


def casar_vendedores(linhas_pdf, vendedores_df):
    """Casa as linhas extraídas do PDF (nome_pdf, n_ped, t_liq) com o cadastro de
    vendedores (DataFrame com colunas id/nome/loja), usando normalizar_nome_match.
    Retorna (matched, nao_encontrados) — matched é uma lista de dicts com
    vendedor_id/nome/loja/n_ped/t_liq; nao_encontrados é a lista de nomes do PDF sem
    correspondência no cadastro (nunca cria vendedor novo automaticamente)."""
    mapa_norm = {normalizar_nome_match(row["nome"]): row for _, row in vendedores_df.iterrows()}
    matched, nao_encontrados = [], []
    for linha in linhas_pdf:
        cadastro = mapa_norm.get(normalizar_nome_match(linha["nome_pdf"]))
        if cadastro is not None:
            matched.append({
                "vendedor_id": int(cadastro["id"]),
                "nome": cadastro["nome"],
                "loja": cadastro["loja"],
                "n_ped": linha["n_ped"],
                "t_liq": linha["t_liq"],
            })
        else:
            nao_encontrados.append(linha["nome_pdf"])
    return matched, nao_encontrados