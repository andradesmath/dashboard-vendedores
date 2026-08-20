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


# --------------------------------------------------------------------------
# Parsing do PDF 'Relatório Totais de Vendas Por Produtos' do SGI — relatório
# diferente do de cima: em vez de uma linha por vendedor, tem uma SEÇÃO por
# vendedor (cabeçalho "CÓD - NOME"), cada uma com uma tabela de produtos
# (N°, Cod, Descrição Produto, Marca, Fornecedor, Posit, Vendas, Qtd, Qtd Cx,
# Valor Total, %). O extrator do pypdf entrega esse relatório com UMA CÉLULA
# POR LINHA (não uma linha de texto por linha de tabela), então o parser varre
# a lista de linhas com uma máquina de estados simples em vez de regex único.
#
# Validado extraindo o PDF real enviado pelo usuário (10 seções de vendedor,
# 650 produtos): a soma do "Valor Total" de cada seção bate com o valor
# impresso na linha "Total:" daquela seção em todos os vendedores.
# --------------------------------------------------------------------------
_FORNECEDOR_PAT = re.compile(r"^\d{5} - .+")  # ex.: "00122 - NORTENE MATRIZ"
_VENDEDOR_HEADER_PAT = re.compile(r"^\d{2,3} - .+")  # ex.: "004 - TAMIRES LARCEDA"
_CODIGO_PRODUTO_PAT = re.compile(r"^\d{4,6}$")  # ex.: "01992"
_CABECALHO_COLUNA_PRODUTO = {
    "N°", "Cod", "Descrição Produto", "Marca", "Fornecedor",
    "Posit", "Vendas", "Qtd", "Qtd Cx", "Valor Total", "%",
}
_BOILERPLATE_PRODUTO_PREFIXOS = ("S.G.I.", "Relatorio Totais", "©", "SGI SOLUTION", "- (")


def _extrair_nome_vendedor_header(header):
    """'004 - TAMIRES LARCEDA' -> 'TAMIRES LARCEDA' (remove o código numérico do
    início; se por acaso vier com cargo tipo 'NOME - CARGO' no final, quem resolve
    isso é normalizar_nome_match na hora de casar com o cadastro)."""
    partes = header.split(" - ", 1)
    return partes[1].strip() if len(partes) == 2 else header.strip()


def parse_linhas_relatorio_produtos_pdf(linhas):
    """Varre a lista de linhas (uma célula por linha, como o pypdf entrega esse
    relatório) e monta a lista de produtos vendidos por vendedor. Cada item:
    {vendedor_nome_pdf, cod_produto, descricao_produto, marca, fornecedor, posit,
    vendas, qtd, qtd_cx, valor_total, pct} — os campos numéricos já convertidos
    para float via parse_valor_brl."""
    linhas = [l.strip() for l in linhas if l.strip()]
    produtos = []
    vendedor_atual = None
    i, n = 0, len(linhas)

    while i < n:
        l = linhas[i]

        # cabeçalho de página: "DD/MM/AAAA a DD/MM/AAAA", timestamp+S.G.I., título do relatório
        if re.match(r"^\d{2}/\d{2}/\d{4} a \d{2}/\d{2}/\d{4}$", l) or l.startswith(_BOILERPLATE_PRODUTO_PREFIXOS):
            i += 1
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}", l):
            i += 1
            continue

        # rótulo de coluna (repete a cada página/seção)
        if l in _CABECALHO_COLUNA_PRODUTO:
            i += 1
            continue

        # linha "Total:" de fechamento de seção -> pula ela + os 6 valores agregados
        if l == "Total:":
            i += 1
            consumidos = 0
            while i < n and consumidos < 6 and (PADRAO_NUM_PDF.match(linhas[i]) or linhas[i].endswith("%")):
                i += 1
                consumidos += 1
            continue

        # cabeçalho de vendedor ("NNN - NOME"), nunca confundir com fornecedor (5 dígitos)
        if _VENDEDOR_HEADER_PAT.match(l) and not _FORNECEDOR_PAT.match(l):
            vendedor_atual = _extrair_nome_vendedor_header(l)
            i += 1
            continue

        # linha de produto: N° (numérico) seguido de Cod (4-6 dígitos)
        if PADRAO_NUM_PDF.match(l) and i + 1 < n and _CODIGO_PRODUTO_PAT.match(linhas[i + 1]):
            cod_produto = linhas[i + 1]
            j = i + 2
            desc_partes = []
            limite = j + 30  # protege contra loop infinito se o formato mudar
            while j < n and not _FORNECEDOR_PAT.match(linhas[j]) and j < limite:
                desc_partes.append(linhas[j])
                j += 1
            if j >= n or not _FORNECEDOR_PAT.match(linhas[j]):
                # não achou o padrão esperado (fornecedor) depois da descrição — pula
                # essa linha isolada em vez de abortar o relatório inteiro.
                i += 1
                continue
            descricao_produto = " ".join(p for p in desc_partes).strip()

            campo1 = linhas[j]
            j += 1
            marca, fornecedor = None, campo1
            if j < n and _FORNECEDOR_PAT.match(linhas[j]):
                marca = campo1
                fornecedor = linhas[j]
                j += 1

            if j + 5 >= n:
                i += 1
                continue
            posit, vendas, qtd, qtd_cx, valor_total, pct = linhas[j:j + 6]
            j += 6

            produtos.append({
                "vendedor_nome_pdf": vendedor_atual,
                "cod_produto": cod_produto,
                "descricao_produto": descricao_produto,
                "marca": marca,
                "fornecedor": fornecedor,
                "posit": parse_valor_brl(posit),
                "vendas": parse_valor_brl(vendas),
                "qtd": parse_valor_brl(qtd),
                "qtd_cx": parse_valor_brl(qtd_cx),
                "valor_total": parse_valor_brl(valor_total),
                "pct": parse_valor_brl(pct),
            })
            i = j
            continue

        # linha não reconhecida (ex.: rodapé de página) — ignora e segue
        i += 1

    return produtos


def parse_relatorio_produtos_pdf(arquivo_bytes):
    """Extrai do PDF 'Relatório Totais de Vendas Por Produtos' do SGI: o período do
    relatório e a lista de produtos vendidos por vendedor (nome como aparece no PDF,
    sem casar com o cadastro ainda — isso é feito por casar_produtos_vendedores)."""
    texto = extrair_texto_pdf(arquivo_bytes)

    data_ini = data_fim = None
    m_periodo = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+a\s+(\d{2})/(\d{2})/(\d{4})", texto)
    if m_periodo:
        d1, m1, a1, d2, m2, a2 = m_periodo.groups()
        try:
            data_ini = date(int(a1), int(m1), int(d1))
            data_fim = date(int(a2), int(m2), int(d2))
        except ValueError:
            pass

    produtos = parse_linhas_relatorio_produtos_pdf(texto.splitlines())
    return {"data_ini": data_ini, "data_fim": data_fim, "produtos": produtos}


def casar_produtos_vendedores(produtos_pdf, vendedores_df):
    """Casa cada produto extraído (campo vendedor_nome_pdf) com o cadastro de
    vendedores, usando normalizar_nome_match (mesma lógica de casar_vendedores).
    Retorna (matched, nao_encontrados) — matched é a mesma lista de produtos com
    vendedor_id/nome/loja adicionados; nao_encontrados é a lista (única) de nomes
    do PDF sem correspondência no cadastro."""
    mapa_norm = {normalizar_nome_match(row["nome"]): row for _, row in vendedores_df.iterrows()}
    matched, nao_encontrados_set = [], set()
    for produto in produtos_pdf:
        nome_pdf = produto.get("vendedor_nome_pdf") or ""
        cadastro = mapa_norm.get(normalizar_nome_match(nome_pdf))
        if cadastro is not None:
            item = dict(produto)
            item["vendedor_id"] = int(cadastro["id"])
            item["nome"] = cadastro["nome"]
            item["loja"] = cadastro["loja"]
            matched.append(item)
        else:
            nao_encontrados_set.add(nome_pdf)
    return matched, sorted(nao_encontrados_set)