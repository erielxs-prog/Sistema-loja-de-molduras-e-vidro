"""
Sistema de Gestão - CIR Molduras
(clientes, orçamentos configuráveis, produtos/estoque, vendas e agenda)

Instalar:   pip install flask
Executar:   python app.py      (abre sozinho em http://127.0.0.1:5000)

Os dados ficam no arquivo gestao.db, na mesma pasta. Use o link "Backup" no
menu para baixar uma cópia. Para trocar o nome da empresa, edite NOME_EMPRESA.
Se você já usava a versão anterior, o gestao.db é atualizado automaticamente
(nada é perdido).
"""
import os
import re
import sqlite3
import sys
import threading
import uuid
import webbrowser
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, InvalidOperation

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, url_for)
from jinja2 import DictLoader

NOME_EMPRESA = "CIR Molduras"
# Rodando como .exe (PyInstaller) o __file__ aponta para uma pasta temporária: os dados ficam ao lado do .exe.
PASTA = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PASTA, "gestao.db")

UNIDADES = {"un": "un", "m2": "m²", "ml": "ML"}           # código -> rótulo curto
ORC = {  # status do orçamento -> (rótulo, cor)
    "aberto": ("Em aberto", "wa"),
    "aprovado": ("Aprovado", "ok"),
    "producao": ("Em produção", "wa"),
    "concluido": ("Concluído / entregue", "ok"),
    "recusado": ("Cancelado / recusado", "er"),
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.secret_key = os.urandom(24)


# ------------------------------ Banco de dados ---------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, telefone TEXT,
  email TEXT, obs TEXT, criado_em TEXT, cidade TEXT, endereco TEXT);
CREATE TABLE IF NOT EXISTS produtos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, sku TEXT,
  preco INTEGER NOT NULL DEFAULT 0, estoque REAL NOT NULL DEFAULT 0,
  estoque_min REAL NOT NULL DEFAULT 0, ativo INTEGER NOT NULL DEFAULT 1,
  unidade TEXT NOT NULL DEFAULT 'un', servico INTEGER NOT NULL DEFAULT 0,
  custo INTEGER NOT NULL DEFAULT 0, categoria TEXT NOT NULL DEFAULT '', espessura REAL);
CREATE TABLE IF NOT EXISTS vendas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER, data TEXT,
  total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ativa', obs TEXT,
  FOREIGN KEY(cliente_id) REFERENCES clientes(id));
CREATE TABLE IF NOT EXISTS itens_venda(
  id INTEGER PRIMARY KEY AUTOINCREMENT, venda_id INTEGER NOT NULL,
  produto_id INTEGER NOT NULL, qtd INTEGER NOT NULL, preco_unit INTEGER NOT NULL,
  FOREIGN KEY(venda_id) REFERENCES vendas(id),
  FOREIGN KEY(produto_id) REFERENCES produtos(id));
CREATE TABLE IF NOT EXISTS agenda(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER, titulo TEXT NOT NULL,
  data_hora TEXT NOT NULL, obs TEXT, status TEXT NOT NULL DEFAULT 'agendado',
  FOREIGN KEY(cliente_id) REFERENCES clientes(id));
CREATE TABLE IF NOT EXISTS orcamentos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id INTEGER NOT NULL,
  titulo TEXT, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'aberto',
  med_l REAL, med_a REAL, med_p REAL, perda REAL NOT NULL DEFAULT 0,
  desconto INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
  prazo TEXT, obs TEXT, aprovado_em TEXT, estoque_baixado INTEGER NOT NULL DEFAULT 0, token TEXT,
  FOREIGN KEY(cliente_id) REFERENCES clientes(id));
CREATE TABLE IF NOT EXISTS orcamento_itens(
  id INTEGER PRIMARY KEY AUTOINCREMENT, orcamento_id INTEGER NOT NULL,
  produto_id INTEGER, descricao TEXT NOT NULL, unidade TEXT NOT NULL DEFAULT 'un',
  larg REAL, alt REAL, pecas REAL, qtd REAL NOT NULL,
  preco_unit INTEGER NOT NULL, subtotal INTEGER NOT NULL,
  linha_id INTEGER, categoria TEXT NOT NULL DEFAULT '', qtd_unit REAL, sub_unit INTEGER,
  larg_ov REAL, alt_ov REAL, manual INTEGER NOT NULL DEFAULT 0, espessura REAL, perda REAL,
  FOREIGN KEY(orcamento_id) REFERENCES orcamentos(id),
  FOREIGN KEY(produto_id) REFERENCES produtos(id));
CREATE TABLE IF NOT EXISTS orc_linhas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, orcamento_id INTEGER NOT NULL,
  ordem INTEGER NOT NULL DEFAULT 0, nome TEXT NOT NULL, descricao TEXT,
  largura REAL, altura REAL, qtd INTEGER NOT NULL DEFAULT 1,
  perda REAL NOT NULL DEFAULT 0, valor_unit INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(orcamento_id) REFERENCES orcamentos(id));
CREATE TABLE IF NOT EXISTS mov_estoque(
  id INTEGER PRIMARY KEY AUTOINCREMENT, produto_id INTEGER NOT NULL, data TEXT NOT NULL,
  tipo TEXT NOT NULL, qtd REAL NOT NULL, saldo_apos REAL NOT NULL,
  orcamento_id INTEGER, venda_id INTEGER,
  FOREIGN KEY(produto_id) REFERENCES produtos(id));
"""


EMPRESA_SCHEMA = """
CREATE TABLE IF NOT EXISTS empresa(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  razao TEXT, fantasia TEXT, cnpj TEXT, ie TEXT, im TEXT,
  telefone TEXT, whatsapp TEXT, email TEXT, site TEXT,
  cep TEXT, logradouro TEXT, numero TEXT, complemento TEXT, bairro TEXT, cidade TEXT, estado TEXT,
  vendedor TEXT, cargo TEXT, obs_inst TEXT, rodape TEXT,
  logo BLOB, logo_tipo TEXT, atualizado_em TEXT);
"""

EXTRATO_SCHEMA = """
CREATE TABLE IF NOT EXISTS extratos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, banco TEXT NOT NULL, conta TEXT, titular TEXT,
  ini TEXT, fim TEXT, arquivo TEXT, hash TEXT NOT NULL, saldo_ant INTEGER, saldo_final INTEGER,
  confere INTEGER NOT NULL DEFAULT 0, criado_em TEXT);
CREATE TABLE IF NOT EXISTS extrato_linhas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, extrato_id INTEGER NOT NULL, chave TEXT NOT NULL,
  data TEXT NOT NULL, historico TEXT NOT NULL, detalhe TEXT, valor INTEGER NOT NULL,
  sinal TEXT NOT NULL, acao TEXT NOT NULL DEFAULT 'decidir', categoria_id INTEGER, venda_id INTEGER,
  lancamento_id INTEGER, aplicado INTEGER NOT NULL DEFAULT 0, nota TEXT,
  FOREIGN KEY(extrato_id) REFERENCES extratos(id));
CREATE INDEX IF NOT EXISTS idx_extlin_chave ON extrato_linhas(chave);
CREATE TABLE IF NOT EXISTS banco_categorias(
  id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, tipo TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bcat ON banco_categorias(lower(nome), tipo);
CREATE TABLE IF NOT EXISTS banco_saldos(
  banco TEXT NOT NULL, mes TEXT NOT NULL, saldo INTEGER NOT NULL, atualizado_em TEXT, PRIMARY KEY(banco, mes));
CREATE TABLE IF NOT EXISTS extrato_regras(
  chave TEXT PRIMARY KEY, acao TEXT NOT NULL, categoria_id INTEGER);
"""

FIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS fin_categorias(
  id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, tipo TEXT NOT NULL,
  grupo TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS lancamentos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL, descricao TEXT NOT NULL,
  categoria_id INTEGER, valor INTEGER NOT NULL, data TEXT NOT NULL, vencimento TEXT,
  pagamento TEXT, status TEXT NOT NULL DEFAULT 'pendente', obs TEXT, criado_em TEXT,
  FOREIGN KEY(categoria_id) REFERENCES fin_categorias(id));
CREATE TABLE IF NOT EXISTS venda_custos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, venda_id INTEGER NOT NULL, produto_id INTEGER,
  descricao TEXT, qtd REAL, custo_unit INTEGER, subtotal INTEGER,
  conhecido INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS venda_eventos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, venda_id INTEGER NOT NULL, data TEXT NOT NULL,
  evento TEXT NOT NULL, total_antes INTEGER, total_depois INTEGER, obs TEXT);
"""

# grupo define onde a categoria entra na DRE: operacional, deducao (tributos/deduções da receita),
# outras (não operacional) ou estoque (compra de material: NÃO é despesa, o custo entra no CPV).
CATEGORIAS_PADRAO = (
    [("despesa", n, "operacional") for n in (
        "Aluguel", "Água", "Energia elétrica", "Internet", "Telefone", "Salários e encargos",
        "Transporte e frete", "Embalagens", "Manutenção", "Marketing e publicidade",
        "Taxas de plataformas", "Despesas administrativas")]
    + [("despesa", "Impostos", "deducao"), ("despesa", "Outras despesas", "operacional"),
       ("despesa", "Compra de materiais (estoque)", "estoque"),
       ("receita", "Prestação de serviços", "operacional"),
       ("receita", "Receitas operacionais diversas", "operacional"),
       ("receita", "Outras receitas", "outras")])


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def norm_tel(t):
    """Telefone para comparação: só dígitos; +55 / 55 / 0 de operadora são removidos para números
    brasileiros; número internacional (+ outro país) fica com '+' e o código do país."""
    t = (t or "").strip()
    d = re.sub(r"\D", "", t)
    if not d:
        return None
    if t.startswith("00"):
        d = d[2:]
    if (t.startswith("+") or t.startswith("00")) and not d.startswith("55"):
        return "+" + d
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    elif d.startswith("0") and len(d) in (11, 12):
        d = d[1:]
    return d


def chave_cat(nome):
    """Chave para achar categorias equivalentes: sem maiúsculas, acentos, espaços extras nem plural simples."""
    import unicodedata
    n = unicodedata.normalize("NFKD", (nome or "").strip().lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\s+", " ", n)
    return n[:-1] if len(n) > 3 and n.endswith("s") else n


CATEGORIAS_SUGERIDAS = ("Molduras", "Vidros", "Espelhos", "MDF", "Fundos", "Paspatur", "Ferragens",
                        "Acessórios", "Serviços", "Outros")


def migrar_cadastros(con):
    """Categorias de produto (tabela própria), SKU único e telefone único. Sem apagar nada:
    se já houver duplicidades antigas, a restrição só é criada depois que forem corrigidas
    (veja a página Duplicidades) e, até lá, o sistema bloqueia novos duplicados pelo código."""
    existia = con.execute("SELECT 1 FROM sqlite_master WHERE name='categorias_produto'").fetchone()
    con.execute("CREATE TABLE IF NOT EXISTS categorias_produto(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "nome TEXT NOT NULL, ativo INTEGER NOT NULL DEFAULT 1)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_catprod_nome ON categorias_produto(lower(trim(nome)))")
    if "categoria_id" not in {r[1] for r in con.execute("PRAGMA table_info(produtos)")}:
        con.execute("ALTER TABLE produtos ADD COLUMN categoria_id INTEGER REFERENCES categorias_produto(id)")
    # produtos com categoria em texto, ainda sem vínculo: cria/reutiliza a categoria equivalente
    nomes = {}
    for cid, nome in con.execute("SELECT id, nome FROM categorias_produto ORDER BY id"):
        nomes.setdefault(chave_cat(nome), cid)
    for pid, cat in con.execute("SELECT id, categoria FROM produtos WHERE categoria_id IS NULL "
                                "AND trim(COALESCE(categoria,''))<>'' ORDER BY id").fetchall():
        k = chave_cat(cat)
        if k not in nomes:
            nomes[k] = con.execute("INSERT INTO categorias_produto(nome) VALUES(?)",
                                   (re.sub(r"\s+", " ", cat.strip())[:60],)).lastrowid
        canon = con.execute("SELECT nome FROM categorias_produto WHERE id=?", (nomes[k],)).fetchone()[0]
        con.execute("UPDATE produtos SET categoria_id=?, categoria=? WHERE id=?", (nomes[k], canon, pid))
    if not existia:        # sugestões só na criação e sem repetir o que já existe
        for sug in CATEGORIAS_SUGERIDAS:
            if chave_cat(sug) not in nomes:
                nomes[chave_cat(sug)] = con.execute("INSERT INTO categorias_produto(nome) VALUES(?)",
                                                    (sug,)).lastrowid
    # SKU único (sem diferenciar maiúsculas/minúsculas e espaços nas pontas)
    if not con.execute("SELECT 1 FROM produtos WHERE trim(COALESCE(sku,''))<>'' GROUP BY lower(trim(sku)) "
                       "HAVING COUNT(*)>1").fetchone():
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_prod_sku ON produtos(lower(trim(sku))) "
                    "WHERE trim(COALESCE(sku,''))<>''")
    # telefone único (forma normalizada)
    if "telefone_norm" not in {r[1] for r in con.execute("PRAGMA table_info(clientes)")}:
        con.execute("ALTER TABLE clientes ADD COLUMN telefone_norm TEXT")
    for cid, tel in con.execute("SELECT id, telefone FROM clientes WHERE telefone_norm IS NULL "
                                "AND trim(COALESCE(telefone,''))<>''").fetchall():
        con.execute("UPDATE clientes SET telefone_norm=? WHERE id=?", (norm_tel(tel), cid))
    if not con.execute("SELECT 1 FROM clientes WHERE telefone_norm IS NOT NULL AND telefone_norm<>'' "
                       "GROUP BY telefone_norm HAVING COUNT(*)>1").fetchone():
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cli_tel ON clientes(telefone_norm) "
                    "WHERE telefone_norm IS NOT NULL AND telefone_norm<>''")
    # versão leve do logotipo para impressão
    if "logo_print" not in {r[1] for r in con.execute("PRAGMA table_info(empresa)")}:
        con.execute("ALTER TABLE empresa ADD COLUMN logo_print BLOB")
        con.execute("ALTER TABLE empresa ADD COLUMN logo_print_tipo TEXT")
    r = con.execute("SELECT logo, logo_tipo FROM empresa WHERE id=1 AND logo IS NOT NULL "
                    "AND logo_print IS NULL").fetchone()
    if r:
        lp, lt = logo_leve(r[0], r[1])
        if lp:
            con.execute("UPDATE empresa SET logo_print=?, logo_print_tipo=? WHERE id=1", (lp, lt))


def logo_leve(dados, tipo, largura=600):
    """Reduz o logotipo (se o Pillow estiver instalado) para a impressão não carregar arquivo grande."""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(dados))
        if im.width <= largura and len(dados) < 150_000:
            return None, None
        im.thumbnail((largura, largura * 2))
        buf = io.BytesIO()
        if tipo == "image/jpeg":
            im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
        else:
            im.save(buf, "PNG", optimize=True)
        return (buf.getvalue(), tipo) if len(buf.getvalue()) < len(dados) else (None, None)
    except Exception:
        return None, None


def iniciar_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # migração de bancos criados pela versão anterior
    cols = {r[1] for r in con.execute("PRAGMA table_info(produtos)")}
    if "unidade" not in cols:
        con.execute("ALTER TABLE produtos ADD COLUMN unidade TEXT NOT NULL DEFAULT 'un'")
    if "servico" not in cols:
        con.execute("ALTER TABLE produtos ADD COLUMN servico INTEGER NOT NULL DEFAULT 0")
    if "custo" not in cols:
        con.execute("ALTER TABLE produtos ADD COLUMN custo INTEGER NOT NULL DEFAULT 0")
    ocols = {r[1] for r in con.execute("PRAGMA table_info(orcamentos)")}
    if "estoque_baixado" not in ocols:
        con.execute("ALTER TABLE orcamentos ADD COLUMN estoque_baixado INTEGER NOT NULL DEFAULT 0")
    if "token" not in ocols:
        con.execute("ALTER TABLE orcamentos ADD COLUMN token TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orc_token ON orcamentos(token) "
                "WHERE token IS NOT NULL")
    ccols = {r[1] for r in con.execute("PRAGMA table_info(clientes)")}
    for col in ("cidade", "endereco"):
        if col not in ccols:
            con.execute(f"ALTER TABLE clientes ADD COLUMN {col} TEXT")
    if "espessura" not in cols:
        con.execute("ALTER TABLE produtos ADD COLUMN espessura REAL")
    if "categoria" not in cols:
        con.execute("ALTER TABLE produtos ADD COLUMN categoria TEXT NOT NULL DEFAULT ''")
        # sugestão feita uma única vez (você pode mudar em Produtos > Editar):
        # ML vira "Moldura"; m² recebe a primeira palavra do nome (ex.: "Vidro 2mm" -> "Vidro").
        for pid, nome, un in con.execute("SELECT id, nome, unidade FROM produtos "
                                         "WHERE servico=0 AND unidade IN ('m2','ml')").fetchall():
            palavra = (nome.split() or [""])[0]
            cat = "Moldura" if un == "ml" else palavra[:1].upper() + palavra[1:]
            con.execute("UPDATE produtos SET categoria=? WHERE id=?", (cat, pid))
    ocols = {r[1] for r in con.execute("PRAGMA table_info(orcamentos)")}
    for col, tipo in (("desconto_tipo", "TEXT NOT NULL DEFAULT 'valor'"),
                      ("desconto_valor", "REAL"), ("entrega", "TEXT"), ("vendedor", "TEXT")):
        if col not in ocols:
            con.execute(f"ALTER TABLE orcamentos ADD COLUMN {col} {tipo}")
    # orçamentos antigos: o desconto era sempre em R$ (valor original = valor aplicado)
    con.execute("UPDATE orcamentos SET desconto_valor=desconto/100.0 WHERE desconto_valor IS NULL")
    lcols = {r[1] for r in con.execute("PRAGMA table_info(orc_linhas)")}
    for col, tipo in (("largura_calc", "REAL"), ("altura_calc", "REAL"),
                      ("arred5", "INTEGER NOT NULL DEFAULT 0")):   # 0 = orçamento antigo (sem arredondar)
        if col not in lcols:
            con.execute(f"ALTER TABLE orc_linhas ADD COLUMN {col} {tipo}")
    pcols = {r[1] for r in con.execute("PRAGMA table_info(produtos)")}
    if "minimo" not in pcols:
        con.execute("ALTER TABLE produtos ADD COLUMN minimo INTEGER")
    if "concluido_em" not in {r[1] for r in con.execute("PRAGMA table_info(orcamentos)")}:
        con.execute("ALTER TABLE orcamentos ADD COLUMN concluido_em TEXT")
    ocols = {r[1] for r in con.execute("PRAGMA table_info(orcamentos)")}
    for col, tipo in (("aval_enviada_em", "TEXT"), ("aval_dispensada", "INTEGER NOT NULL DEFAULT 0")):
        if col not in ocols:
            con.execute(f"ALTER TABLE orcamentos ADD COLUMN {col} {tipo}")
    con.execute("CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT)")
    vcols = {r[1] for r in con.execute("PRAGMA table_info(vendas)")}
    for col, tipo in (("orcamento_id", "INTEGER"), ("valor_bruto", "INTEGER"),
                      ("desconto", "INTEGER"), ("cpv", "INTEGER"),
                      ("cpv_pendentes", "INTEGER"), ("recebido_em", "TEXT")):
        if col not in vcols:
            con.execute(f"ALTER TABLE vendas ADD COLUMN {col} {tipo}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vendas_orc ON vendas(orcamento_id) "
                "WHERE orcamento_id IS NOT NULL")
    con.executescript(FIN_SCHEMA)
    con.executescript(EMPRESA_SCHEMA)
    migrou_bancos = con.execute("SELECT 1 FROM sqlite_master WHERE name='banco_categorias'").fetchone()
    con.executescript(EXTRATO_SCHEMA)
    if "extrato_linha_id" not in {r[1] for r in con.execute("PRAGMA table_info(lancamentos)")}:
        con.execute("ALTER TABLE lancamentos ADD COLUMN extrato_linha_id INTEGER")
    if not migrou_bancos:
        for tabela in ("extrato_linhas", "extrato_regras"):
            for rid, cid in con.execute(f"SELECT {'id' if tabela == 'extrato_linhas' else 'rowid'}, categoria_id "
                                        f"FROM {tabela} WHERE categoria_id IS NOT NULL").fetchall():
                c = con.execute("SELECT nome, tipo FROM fin_categorias WHERE id=?", (cid,)).fetchone()
                novo = None
                if c:
                    con.execute("INSERT OR IGNORE INTO banco_categorias(nome,tipo) VALUES(?,?)", (c[0], c[1]))
                    novo = con.execute("SELECT id FROM banco_categorias WHERE lower(nome)=lower(?) AND tipo=?",
                                       (c[0], c[1])).fetchone()[0]
                con.execute(f"UPDATE {tabela} SET categoria_id=? WHERE {'id' if tabela == 'extrato_linhas' else 'rowid'}=?",
                            (novo, rid))
        # lançamentos que o extrato tinha criado no Financeiro saem de lá (evita contar duas vezes):
        # os movimentos continuam nas linhas do extrato e passam a aparecer na página Bancos.
        con.execute("DELETE FROM lancamentos WHERE extrato_linha_id IS NOT NULL")
        con.execute("UPDATE extrato_linhas SET lancamento_id=NULL")
        con.execute("UPDATE extrato_linhas SET venda_id=NULL, acao='decidir', aplicado=0 WHERE acao='vincular'")
    if "banco" not in {r[1] for r in con.execute("PRAGMA table_info(lancamentos)")}:
        con.execute("ALTER TABLE lancamentos ADD COLUMN banco TEXT")
    con.execute("INSERT OR IGNORE INTO empresa(id) VALUES(1)")
    migrar_cadastros(con)
    if not con.execute("SELECT 1 FROM fin_categorias LIMIT 1").fetchone():
        con.executemany("INSERT INTO fin_categorias(nome,tipo,grupo) VALUES(?,?,?)",
                        [(n, t, g) for t, n, g in CATEGORIAS_PADRAO])
    icols = {r[1] for r in con.execute("PRAGMA table_info(orcamento_itens)")}
    novas = {"minimo": "INTEGER", "calc_unit": "INTEGER", "desc_rateio": "INTEGER NOT NULL DEFAULT 0",
             "linha_id": "INTEGER", "categoria": "TEXT NOT NULL DEFAULT ''", "qtd_unit": "REAL",
             "sub_unit": "INTEGER", "larg_ov": "REAL", "alt_ov": "REAL",
             "manual": "INTEGER NOT NULL DEFAULT 0", "espessura": "REAL", "perda": "REAL"}
    for col, tipo in novas.items():
        if col not in icols:
            con.execute(f"ALTER TABLE orcamento_itens ADD COLUMN {col} {tipo}")
    converter_itens_antigos(con)
    con.row_factory = sqlite3.Row
    for o in con.execute("SELECT id, aprovado_em, data FROM orcamentos WHERE status='concluido' "
                         "AND id NOT IN (SELECT orcamento_id FROM vendas "
                         "WHERE orcamento_id IS NOT NULL)").fetchall():
        quando = o["aprovado_em"] or o["data"]
        con.execute("UPDATE orcamentos SET concluido_em=COALESCE(concluido_em, ?) WHERE id=?",
                    (quando, o["id"]))
        registrar_venda(con, o["id"], quando=quando, origem="importada ao atualizar o sistema")
    con.commit()
    con.close()


def converter_itens_antigos(con):
    """Orçamentos da versão anterior: cada item vira um produto do orçamento com 1 componente.
    Valores, quantidades e preços ficam exatamente como estavam (e o estoque não é tocado)."""
    con.row_factory = sqlite3.Row
    ordem = {}
    for r in con.execute("SELECT i.*, o.med_l, o.med_a FROM orcamento_itens i "
                         "JOIN orcamentos o ON o.id=i.orcamento_id WHERE i.linha_id IS NULL "
                         "ORDER BY i.orcamento_id, i.id").fetchall():
        oid = r["orcamento_id"]
        ordem[oid] = ordem.get(oid, 0) + 1
        cur = con.execute(
            "INSERT INTO orc_linhas(orcamento_id,ordem,nome,descricao,largura,altura,qtd,perda,"
            "valor_unit,total) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (oid, ordem[oid], r["descricao"], "", r["larg"] or r["med_l"], r["alt"] or r["med_a"],
             1, 0, r["subtotal"], r["subtotal"]))
        con.execute("UPDATE orcamento_itens SET linha_id=?, qtd_unit=qtd, sub_unit=subtotal, "
                    "manual=1 WHERE id=?", (cur.lastrowid, r["id"]))
    con.row_factory = None


# ------------------------------- Utilidades ------------------------------
def centavos(texto):
    """'12,50' / '1.234,50' / '12.5' -> 1250 (inteiro, em centavos)."""
    s = (texto or "0").strip().replace("R$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return int(round(float(s) * 100))


def decimal(texto):
    """'42,5' -> 42.5 (levanta ValueError se inválido)."""
    return float((texto or "").strip().replace(",", "."))


def decimal_opc(texto):
    t = (texto or "").strip()
    return decimal(t) if t else None


def inteiro(texto, padrao=0):
    try:
        return int(str(texto).strip() or padrao)
    except ValueError:
        return padrao


D0 = Decimal(0)


def dec(v):
    """Texto/número -> Decimal (aceita vírgula). Vazio -> None. Inválido -> ValueError."""
    if v is None or isinstance(v, bool):
        if isinstance(v, bool):
            raise ValueError(v)
        return None
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    else:
        t = str(v).strip().replace(" ", "").replace("R$", "")
        if t == "":
            return None
        if "," in t:
            t = t.replace(".", "").replace(",", ".")
        try:
            d = Decimal(t)
        except InvalidOperation:
            raise ValueError(v)
    if not d.is_finite():
        raise ValueError(v)
    return d


def arred(d, casas=0):
    """Arredonda (meio para cima) em `casas` casas decimais."""
    return d.quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP)


def multiplo5(x):
    """Próximo múltiplo de 5 (sempre para cima; múltiplo exato fica igual): 21 -> 25, 20 -> 20."""
    return (x / 5).to_integral_value(rounding=ROUND_CEILING) * 5


def para_centavos(v):
    d = dec(v)
    return None if d is None else int(arred(d * 100))


def fmt_qtd(q, unidade="un"):
    """0.075 -> '0,075' ; 1.1 (ML) -> '1,10' ; 3 (un) -> '3'."""
    d = Decimal(str(q))
    inteiro_s, _, frac = f"{d:.4f}".rstrip("0").partition(".")
    frac = frac.ljust(2 if unidade in ("m2", "ml") else 0, "0")
    return inteiro_s + ("," + frac if frac else "")


def dinheiro(c):
    """1250 -> '12,50' (para preencher campos)."""
    return f"{(c or 0) / 100:.2f}".replace(".", ",")


def mover_estoque(db, produto_id, delta, tipo, orcamento_id=None, venda_id=None):
    """Altera o saldo (que PODE ficar negativo) e registra a movimentação."""
    db.execute("UPDATE produtos SET estoque=ROUND(estoque+?,4) WHERE id=?", (delta, produto_id))
    saldo = db.execute("SELECT estoque FROM produtos WHERE id=?", (produto_id,)).fetchone()[0]
    db.execute("INSERT INTO mov_estoque(produto_id,data,tipo,qtd,saldo_apos,orcamento_id,venda_id) "
               "VALUES(?,?,?,?,?,?,?)",
               (produto_id, datetime.now().isoformat(" ", "seconds"), tipo, round(delta, 4),
                saldo, orcamento_id, venda_id))


def controla_estoque(p):
    """Todo produto controla estoque, exceto serviços."""
    return not p["servico"]


@app.template_filter("brl")
def brl(c):
    v = (c or 0) / 100
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + s


@app.template_filter("num")
def num_f(v, casas=3):
    if v is None or v == "":
        return ""
    try:
        v = float(str(v).replace(",", "."))
    except ValueError:
        return v
    s = f"{v:.{casas}f}".rstrip("0").rstrip(".")
    return (s or "0").replace(".", ",")


@app.template_filter("qtd")
def qtd_f(v, un="un"):
    return fmt_qtd(v, un) if v is not None else ""


@app.template_filter("ul")
def ul(u):
    return UNIDADES.get(u, u)


@app.template_filter("dia")
def dia(s):
    """'2026-10-24 10:00:00' -> '24/10/2026' ('' se vazio)."""
    if not s:
        return ""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(s)


@app.template_filter("dt")
def dt(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return s


CAMPOS_EMPRESA = ("razao", "fantasia", "cnpj", "ie", "im", "telefone", "whatsapp", "email", "site",
                  "cep", "logradouro", "numero", "complemento", "bairro", "cidade", "estado",
                  "vendedor", "cargo", "obs_inst", "rodape")
LOGO_MAX = 1024 * 1024
UFS = ("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO").split()


def empresa_dados(db):
    """Dados da empresa para telas e impressão. Nada é inventado: campo vazio fica fora."""
    r = db.execute("SELECT * FROM empresa WHERE id=1").fetchone()
    e = {k: ((r[k] or "").strip() if r else "") for k in CAMPOS_EMPRESA}
    e["nome"] = e["fantasia"] or e["razao"] or NOME_EMPRESA
    e["razao_extra"] = e["razao"] if e["razao"] and e["razao"] != e["nome"] else ""
    rua = ", ".join(x for x in (e["logradouro"], e["numero"]) if x)
    if e["complemento"]:
        rua += (" - " if rua else "") + e["complemento"]
    if e["bairro"]:
        rua += (" - " if rua else "") + e["bairro"]
    cid = "/".join(x for x in (e["cidade"], e["estado"]) if x)
    if e["cep"]:
        cid += (" · " if cid else "") + "CEP " + e["cep"]
    e["linha_rua"], e["linha_cidade"] = rua, cid
    e["fones"] = " / ".join(x for x in (e["telefone"], ("WhatsApp " + e["whatsapp"]) if e["whatsapp"] else "") if x)
    e["tem_logo"] = bool(r and r["logo"])
    e["versao"] = (r["atualizado_em"] or "0") if r else "0"
    return e


@app.context_processor
def contexto():
    try:
        emp = empresa_dados(get_db())
    except sqlite3.Error:
        emp = {"nome": NOME_EMPRESA, "vendedor": "", "tem_logo": False}
    return {"empresa": emp["nome"], "emp": emp, "ORC": ORC}


# -------------------------------- Financeiro -----------------------------
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
         "Setembro", "Outubro", "Novembro", "Dezembro"]
STATUS_LANC = {"despesa": ("pendente", "pago"), "receita": ("pendente", "recebido")}


def periodo(ano, mes):
    """Prefixo de data: 'AAAA-MM' (um mês) ou 'AAAA' (ano todo, mes=0)."""
    return f"{ano:04d}-{mes:02d}" if mes else f"{ano:04d}"


def ler_periodo(args):
    hoje = datetime.now()
    ano = inteiro(args.get("ano"), hoje.year)
    mes = inteiro(args.get("mes"), hoje.month) if args.get("mes") not in (None, "") else hoje.month
    if not 2000 <= ano <= 2100:
        ano = hoje.year
    if not 0 <= mes <= 12:
        mes = hoje.month
    return ano, mes


def dre_periodo(db, px):
    """DRE gerencial (regime de competência) a partir dos dados reais.
    Venda concluída = receita no dia da conclusão; recebimento é outra coisa (ver 'caixa')."""
    n = len(px)
    v = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(COALESCE(NULLIF(valor_bruto,0), total+COALESCE(desconto,0))),0) bruto, "
        "COALESCE(SUM(COALESCE(desconto,0)),0) desc_, COALESCE(SUM(total),0) liq, "
        "COALESCE(SUM(COALESCE(cpv,0)),0) cpv, COALESCE(SUM(COALESCE(cpv_pendentes,0)),0) pend, "
        "COALESCE(SUM(CASE WHEN valor_bruto IS NULL THEN 1 ELSE 0 END),0) legado "
        "FROM vendas WHERE status='ativa' AND substr(data,1,?)=?", (n, px)).fetchone()
    l = {}
    for r in db.execute(
            "SELECT l.tipo, COALESCE(c.grupo,'operacional') grupo, COALESCE(SUM(l.valor),0) t, "
            "COALESCE(SUM(CASE WHEN l.status IN ('pendente') THEN l.valor ELSE 0 END),0) pend "
            "FROM lancamentos l LEFT JOIN fin_categorias c ON c.id=l.categoria_id "
            "WHERE substr(l.data,1,?)=? GROUP BY l.tipo, COALESCE(c.grupo,'operacional')", (n, px)):
        l[(r["tipo"], r["grupo"])] = (r["t"], r["pend"])
    g = lambda t, gr: l.get((t, gr), (0, 0))[0]
    deducoes = g("despesa", "deducao")
    desp_op = g("despesa", "operacional")
    desp_out = g("despesa", "outras")
    rec_out = g("receita", "operacional") + g("receita", "outras")
    receita_liq = v["bruto"] - v["desc_"] - deducoes
    lucro_bruto = receita_liq - v["cpv"]
    resultado_op = lucro_bruto - desp_op
    outras = rec_out - desp_out
    liquido = resultado_op + outras
    incompleto = bool(v["pend"] or v["legado"])
    pend_desp = sum(x[1] for k, x in l.items() if k[0] == "despesa" and k[1] in ("operacional", "outras"))
    # caixa realizado (nada de data inventada: só entra o que foi marcado como recebido/pago)
    caixa = db.execute(
        "SELECT (SELECT COALESCE(SUM(total),0) FROM vendas WHERE status='ativa' AND substr(recebido_em,1,?)=?) vendas, "
        "(SELECT COALESCE(SUM(valor),0) FROM lancamentos WHERE tipo='receita' AND status='recebido' AND substr(pagamento,1,?)=?) rec, "
        "(SELECT COALESCE(SUM(valor),0) FROM lancamentos WHERE tipo='despesa' AND status='pago' AND substr(pagamento,1,?)=?) desp, "
        "(SELECT COALESCE(SUM(total),0) FROM vendas WHERE status='ativa' AND recebido_em IS NULL AND substr(data,1,?)=?) areceber",
        (n, px) * 4).fetchone()
    return {
        "vendas_qtd": v["n"], "vendas_liq": v["liq"], "receita_bruta": v["bruto"],
        "descontos": v["desc_"], "deducoes": deducoes, "receita_liquida": receita_liq,
        "cpv": v["cpv"], "cpv_pendentes": v["pend"], "vendas_sem_custo": v["legado"],
        "cpv_incompleto": incompleto, "lucro_bruto": lucro_bruto,
        "despesas_operacionais": desp_op, "resultado_operacional": resultado_op,
        "outras_receitas": rec_out, "outras_despesas": desp_out, "outras_liquido": outras,
        "resultado_liquido": liquido,
        "despesas_total": desp_op + desp_out, "despesas_pendentes": pend_desp,
        "receitas_total": v["liq"] + rec_out, "despesas_card": desp_op + desp_out + deducoes,
        "caixa": {"vendas_recebidas": caixa["vendas"], "receitas_recebidas": caixa["rec"],
                  "despesas_pagas": caixa["desp"], "vendas_a_receber": caixa["areceber"],
                  "saldo": caixa["vendas"] + caixa["rec"] - caixa["desp"]},
    }


@app.route("/api/painel")
def api_painel():
    db = get_db()
    ano, mes = ler_periodo(request.args)
    d = dre_periodo(db, periodo(ano, mes))
    serie = []
    for m in range(1, 13):
        r = db.execute("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM vendas "
                       "WHERE status='ativa' AND substr(data,1,7)=?", (periodo(ano, m),)).fetchone()
        serie.append({"mes": m, "nome": MESES[m - 1], "total": r["t"], "qtd": r["n"]})
    d["ticket_medio"] = int(arred(Decimal(d["vendas_liq"]) / d["vendas_qtd"])) if d["vendas_qtd"] else 0
    return jsonify(ano=ano, mes=mes, dre=d, serie=serie)


def _lanc_dados(db, f, atual=None):
    """Valida e normaliza um lançamento. Devolve (dados, erros)."""
    erros = []
    tipo = (f.get("tipo") or "").strip()
    if tipo not in STATUS_LANC:
        erros.append("Tipo inválido.")
    desc = (f.get("descricao") or "").strip()
    if not desc:
        erros.append("Informe a descrição.")
    valor = para_centavos(f.get("valor"))
    if valor is None or valor <= 0:
        erros.append("Informe um valor maior que zero.")
    cat = inteiro(f.get("categoria_id"), 0)
    c = db.execute("SELECT * FROM fin_categorias WHERE id=? AND tipo=?", (cat, tipo)).fetchone()
    if not c:
        erros.append("Escolha uma categoria.")

    def data_(k, obrig=False):
        t = (f.get(k) or "").strip()
        if not t:
            if obrig:
                erros.append("Informe a data.")
            return None
        try:
            datetime.strptime(t, "%Y-%m-%d")
        except ValueError:
            erros.append("Data inválida.")
        return t
    data = data_("data", True)
    venc = data_("vencimento")
    pag = data_("pagamento")
    status = (f.get("status") or "pendente").strip().lower()
    if tipo in STATUS_LANC and status not in STATUS_LANC[tipo]:
        erros.append("Situação inválida.")
    if status in ("pago", "recebido") and not pag:
        erros.append("Informe a data do " + ("pagamento." if tipo == "despesa" else "recebimento."))
    if status == "pendente":
        pag = None
    return {"tipo": tipo, "descricao": desc, "categoria_id": cat, "valor": valor, "data": data,
            "vencimento": venc, "pagamento": pag, "status": status,
            "obs": (f.get("obs") or "").strip()}, erros


@app.route("/api/financeiro/lancamentos", methods=["GET", "POST"])
def api_lancamentos():
    db = get_db()
    if request.method == "POST":
        d, erros = _lanc_dados(db, request.get_json(silent=True) or {})
        if erros:
            return jsonify(ok=False, erros=sorted(set(erros))), 400
        cur = db.execute(
            "INSERT INTO lancamentos(tipo,descricao,categoria_id,valor,data,vencimento,pagamento,"
            "status,obs,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (d["tipo"], d["descricao"], d["categoria_id"], d["valor"], d["data"], d["vencimento"],
             d["pagamento"], d["status"], d["obs"], agora_txt()))
        db.commit()
        return jsonify(ok=True, id=cur.lastrowid)
    ano, mes = ler_periodo(request.args)
    px = periodo(ano, mes)
    tipo = request.args.get("tipo", "")
    cat = request.args.get("categoria", "")
    linhas = []
    if tipo in ("", "receita", "despesa"):
        q = ("SELECT l.*, c.nome categoria, c.grupo FROM lancamentos l LEFT JOIN fin_categorias c "
             "ON c.id=l.categoria_id WHERE substr(l.data,1,?)=?")
        par = [len(px), px]
        if tipo:
            q += " AND l.tipo=?"; par.append(tipo)
        if cat.isdigit():
            q += " AND l.categoria_id=?"; par.append(int(cat))
        for r in db.execute(q, par):
            linhas.append({"origem": "lancamento", "id": r["id"], "data": r["data"],
                           "descricao": r["descricao"], "categoria": r["categoria"] or "",
                           "categoria_id": r["categoria_id"], "tipo": r["tipo"], "valor": r["valor"],
                           "status": r["status"], "vencimento": r["vencimento"],
                           "pagamento": r["pagamento"], "obs": r["obs"] or "",
                           "grupo": r["grupo"]})
    if tipo in ("", "venda") and (not cat or cat == "venda"):
        for r in db.execute("SELECT v.*, c.nome cliente FROM vendas v LEFT JOIN clientes c "
                            "ON c.id=v.cliente_id WHERE v.status='ativa' AND substr(v.data,1,?)=?",
                            (len(px), px)):
            linhas.append({"origem": "venda", "id": r["id"], "data": r["data"][:10],
                           "descricao": f"Venda #{r['id']} — {r['cliente'] or 'Consumidor'}",
                           "categoria": "Vendas", "tipo": "venda", "valor": r["total"],
                           "status": "recebido" if r["recebido_em"] else "a receber",
                           "pagamento": r["recebido_em"]})
    linhas.sort(key=lambda x: (x["data"], x["id"]), reverse=True)
    return jsonify(linhas=linhas)


@app.route("/api/financeiro/lancamentos/<int:lid>", methods=["PUT", "DELETE"])
def api_lancamento(lid):
    db = get_db()
    if not db.execute("SELECT 1 FROM lancamentos WHERE id=?", (lid,)).fetchone():
        return jsonify(ok=False, erros=["Lançamento não encontrado."]), 404
    if request.method == "DELETE":
        db.execute("DELETE FROM lancamentos WHERE id=?", (lid,))
        db.commit()
        return jsonify(ok=True)
    d, erros = _lanc_dados(db, request.get_json(silent=True) or {})
    if erros:
        return jsonify(ok=False, erros=sorted(set(erros))), 400
    db.execute("UPDATE lancamentos SET tipo=?,descricao=?,categoria_id=?,valor=?,data=?,vencimento=?,"
               "pagamento=?,status=?,obs=? WHERE id=?",
               (d["tipo"], d["descricao"], d["categoria_id"], d["valor"], d["data"], d["vencimento"],
                d["pagamento"], d["status"], d["obs"], lid))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/financeiro/categorias", methods=["GET", "POST"])
def api_categorias():
    db = get_db()
    if request.method == "POST":
        f = request.get_json(silent=True) or {}
        nome = (f.get("nome") or "").strip()
        tipo = f.get("tipo")
        if tipo not in STATUS_LANC or not nome:
            return jsonify(ok=False, erros=["Informe o nome e o tipo da categoria."]), 400
        if db.execute("SELECT 1 FROM fin_categorias WHERE lower(nome)=lower(?) AND tipo=?",
                      (nome, tipo)).fetchone():
            return jsonify(ok=False, erros=["Essa categoria já existe."]), 400
        grupo = "operacional" if tipo == "despesa" else "outras"
        if f.get("grupo") in ("operacional", "outras", "deducao", "estoque"):
            grupo = f["grupo"]
        cur = db.execute("INSERT INTO fin_categorias(nome,tipo,grupo) VALUES(?,?,?)", (nome, tipo, grupo))
        db.commit()
        return jsonify(ok=True, id=cur.lastrowid)
    return jsonify(categorias=[dict(r) for r in db.execute(
        "SELECT id,nome,tipo,grupo FROM fin_categorias WHERE ativo=1 ORDER BY tipo, nome")])


@app.route("/api/financeiro/categorias/<int:cid>", methods=["PUT", "DELETE"])
def api_categoria(cid):
    db = get_db()
    c = db.execute("SELECT * FROM fin_categorias WHERE id=?", (cid,)).fetchone()
    if not c:
        return jsonify(ok=False, erros=["Categoria não encontrada."]), 404
    if request.method == "DELETE":
        if db.execute("SELECT 1 FROM lancamentos WHERE categoria_id=?", (cid,)).fetchone():
            db.execute("UPDATE fin_categorias SET ativo=0 WHERE id=?", (cid,))   # mantém o histórico
        else:
            db.execute("DELETE FROM fin_categorias WHERE id=?", (cid,))
        db.commit()
        return jsonify(ok=True)
    nome = ((request.get_json(silent=True) or {}).get("nome") or "").strip()
    if not nome:
        return jsonify(ok=False, erros=["Informe o nome."]), 400
    db.execute("UPDATE fin_categorias SET nome=? WHERE id=?", (nome, cid))
    db.commit()
    return jsonify(ok=True)


# -------------------------------- Financeiro (página própria) -------------
@app.route("/financeiro")
def financeiro():
    hoje = datetime.now()
    return render_template("financeiro.html", titulo="Financeiro", ano_atual=hoje.year,
                           mes_atual=hoje.month, meses=MESES)


# -------------------------------- Extrato bancário (PDF) -----------------
import hashlib
import io


class ErroExtrato(Exception):
    pass


RE_CAB = re.compile(r"^\s*(\d\d/\d\d)\s+(\S.*?)\s*$")
RE_VAL = re.compile(r"^(.*?)\s+([\d.]+,\d\d)\s*([CD*])?\s*$")
RE_SO_VAL = re.compile(r"^\s*([\d.]+,\d\d)\s*([CD*])?\s*$")
RE_SO_SINAL = re.compile(r"^\s*([CD*])\s*$")
GENERICAS = ("recebimento pix", "pagamento pix", "transferência pix", "transferencia pix", "doc.", "codigo ted",
             "fav.", "rem.")


def texto_pdf(dados):
    """Texto do PDF mantendo as colunas (precisa do pacote pypdf)."""
    if dados[:5] != b"%PDF-":
        raise ErroExtrato("O arquivo enviado não é um PDF.")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ErroExtrato("Falta instalar o leitor de PDF. No terminal, rode: pip install pypdf")
    try:
        r = PdfReader(io.BytesIO(dados))
        if r.is_encrypted:                    # alguns bancos (Mercado Pago) só bloqueiam edição: senha vazia
            try:
                aberto = r.decrypt("")
            except ImportError:
                raise ErroExtrato("Este PDF é criptografado. No terminal, rode: pip install cryptography")
            except Exception:
                aberto = 0
            if not aberto:
                raise ErroExtrato("O PDF está protegido por senha.")
        partes = []
        for pg in r.pages:
            try:
                partes.append(pg.extract_text(extraction_mode="layout") or "")
            except TypeError:
                raise ErroExtrato("Versão antiga do leitor de PDF. Rode: pip install --upgrade pypdf")
    except ErroExtrato:
        raise
    except Exception:
        raise ErroExtrato("Não consegui abrir este PDF.")
    txt = "\n".join(partes)
    if len(txt.strip()) < 200:
        raise ErroExtrato("O PDF não tem texto legível (parece uma imagem escaneada). "
                          "Baixe o extrato direto do internet banking.")
    return txt


def _cents(v):
    return int(Decimal(v.replace(".", "").replace(",", ".")) * 100)


def ler_sicoob(txt):
    """Extrato 'Conta Corrente' do Sicoob (SISBR). Devolve dict ou levanta ErroExtrato."""
    if "SICOOB" not in txt.upper() or "HISTÓRICO DE MOVIMENTAÇÃO" not in txt.upper():
        raise ErroExtrato("Este PDF não parece um extrato de conta corrente do Sicoob. "
                          "Por enquanto só esse modelo é lido.")
    m = re.search(r"PER[IÍ]ODO:\s*(\d\d)/(\d\d)/(\d{4})\s*-\s*(\d\d)/(\d\d)/(\d{4})", txt)
    if not m:
        raise ErroExtrato("Não encontrei o período do extrato.")
    ini = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    fim = f"{m.group(6)}-{m.group(5)}-{m.group(4)}"
    fim_mes, fim_ano = int(m.group(5)), int(m.group(6))
    conta = re.search(r"CONTA:\s*([^\n/]+?)\s*/\s*([^\n]+)", txt)
    titular = conta.group(2).strip() if conta else ""
    num_conta = conta.group(1).strip() if conta else ""
    corpo = txt.split("HISTÓRICO DE MOVIMENTAÇÃO", 1)[1]
    corpo = re.split(r"\n\s*RESUMO\s*\n", corpo, 1)[0]
    ent, pend = [], None
    for lin in corpo.replace("\f", "\n").split("\n"):
        if not lin.strip() or lin.strip().startswith(("DATA ", "DATA\t")) or lin.strip() == "VALOR":
            continue
        a = RE_SO_VAL.match(lin)
        if a:                                           # valor sozinho (vem antes do lançamento)
            pend = (a.group(1), a.group(2))
            continue
        c = RE_CAB.match(lin)
        if c:
            dia, resto = c.groups()
            v = RE_VAL.match(resto)
            if v:
                hist, val, sin = v.groups()
            else:
                hist, val, sin = resto, None, None
            if val is None and pend:
                val, sin = pend
            pend = None
            ent.append({"dia": dia, "hist": re.sub(r"\s+", " ", hist).strip(), "valor": val,
                        "sinal": sin, "det": []})
            continue
        sg = RE_SO_SINAL.match(lin)
        if sg and ent and ent[-1]["sinal"] is None:
            ent[-1]["sinal"] = sg.group(1)
            continue
        if ent:
            ent[-1]["det"].append(re.sub(r"\s+", " ", lin).strip())
    saldo_ant = None
    linhas, retidos = [], []
    for e in ent:
        h = e["hist"].upper()
        if h.startswith("SALDO ANTERIOR") and e["valor"]:
            saldo_ant = _cents(e["valor"]) * (-1 if e["sinal"] == "D" else 1)
            continue
        if h.startswith("SALDO"):
            continue
        if e["valor"] is None or e["sinal"] is None:
            raise ErroExtrato(f"Não consegui ler o valor da linha de {e['dia']} ({e['hist']}).")
        mes = int(e["dia"][3:])
        ano = fim_ano if mes <= fim_mes else fim_ano - 1
        item = {"data": f"{ano}-{e['dia'][3:]}-{e['dia'][:2]}", "hist": e["hist"],
                "det": [d for d in e["det"] if d], "valor": _cents(e["valor"]), "sinal": e["sinal"]}
        (retidos if e["sinal"] == "*" else linhas).append(item)
    rs = re.search(r"\(\+\)\s*SALDO EM CONTA:\s*([\d.]+,\d\d)\s*([CD])", txt)
    saldo_final = _cents(rs.group(1)) * (-1 if rs.group(2) == "D" else 1) if rs else None
    soma = sum(l["valor"] if l["sinal"] == "C" else -l["valor"] for l in linhas)
    confere = saldo_ant is not None and saldo_final is not None and saldo_ant + soma == saldo_final
    return {"banco": "Sicoob", "conta": num_conta, "titular": titular, "ini": ini, "fim": fim,
            "saldo_ant": saldo_ant, "saldo_final": saldo_final, "confere": confere,
            "linhas": linhas, "retidos": retidos}


RE_MP_LIN = re.compile(r"^\s*(\d\d)-(\d\d)-(\d{4})\s+(.*?)\s{2,}(\d{6,})\s+R\$\s*(-?[\d.]+,\d\d)\s+R\$\s*(-?[\d.]+,\d\d)\s*$")


def _cents_s(v):
    return -_cents(v[1:]) if v.startswith("-") else _cents(v)


def ler_mercadopago(txt):
    """Extrato 'EXTRATO DE CONTA' do Mercado Pago. Devolve o mesmo dict do Sicoob ou levanta ErroExtrato."""
    if "MERCADO PAGO" not in txt.upper() or "DETALHE DOS MOVIMENTOS" not in txt.upper():
        raise ErroExtrato("Este PDF não parece um extrato do Mercado Pago.")
    m = re.search(r"Per[ií]odo:\s*De\s*(\d\d)-(\d\d)-(\d{4})\s*al\s*(\d\d)-(\d\d)-(\d{4})", txt)
    if not m:
        raise ErroExtrato("Não encontrei o período do extrato.")
    ini, fim = f"{m.group(3)}-{m.group(2)}-{m.group(1)}", f"{m.group(6)}-{m.group(5)}-{m.group(4)}"
    ti = re.search(r"EXTRATO DE CONTA\s*\n\s*(\S[^\n]*?)\s*\n", txt)
    titular = ti.group(1).strip() if ti else ""
    ct = re.search(r"Conta:\s*(\d+)", txt)
    conta = ct.group(1) if ct else ""
    si = re.search(r"Saldo inicial:\s*R\$\s*(-?[\d.]+,\d\d)", txt)
    sf = re.search(r"Saldo final:\s*R\$\s*(-?[\d.]+,\d\d)", txt)
    saldo_ant = _cents_s(si.group(1)) if si else None
    saldo_final = _cents_s(sf.group(1)) if sf else None
    corpo = txt.upper().index("DETALHE DOS MOVIMENTOS")
    corpo = txt[corpo:].split("\n", 1)[1]
    corpo = re.split(r"\n\s*Data de gera[cç][aã]o", corpo, 1)[0]
    # o texto de cada lançamento pode vir antes, na própria linha e depois do valor; blocos separados por linha em branco
    blocos, atual = [], []
    for lin in corpo.replace("\f", "\n").split("\n"):
        if re.match(r"^\s*Data\s+Descri", lin) or re.match(r"^\s*\d+/\d+\s*$", lin):
            continue
        if lin.strip():
            atual.append(lin)
        elif atual:
            blocos.append(atual); atual = []
    if atual:
        blocos.append(atual)
    linhas, pre = [], []
    for b in blocos:
        idx = [i for i, l in enumerate(b) if RE_MP_LIN.match(l)]
        if not idx:
            pre += [re.sub(r"\s+", " ", l).strip() for l in b]    # texto que veio antes (ex.: quebra de página)
            continue
        if len(idx) > 1:
            raise ErroExtrato("Não consegui separar os lançamentos de um trecho do extrato.")
        i = idx[0]
        d, mm, a, desc, opid, val, saldo = RE_MP_LIN.match(b[i]).groups()
        partes = pre + [re.sub(r"\s+", " ", l).strip() for l in b[:i]] + [re.sub(r"\s+", " ", desc).strip()] \
            + [re.sub(r"\s+", " ", l).strip() for l in b[i + 1:]]
        pre = []
        v = _cents_s(val)
        linhas.append({"data": f"{a}-{mm}-{d}", "hist": " ".join(x for x in partes if x), "det": [], "id": opid,
                       "valor": abs(v), "sinal": "C" if v >= 0 else "D", "saldo": _cents_s(saldo)})
    if not linhas:
        raise ErroExtrato("Não encontrei movimentações no extrato.")
    # conferência: cada saldo = saldo anterior + valor, e o final bate com o cabeçalho
    corrente, ok = saldo_ant, saldo_ant is not None
    for l in linhas:
        if ok:
            corrente += l["valor"] if l["sinal"] == "C" else -l["valor"]
            ok = corrente == l["saldo"]
    confere = ok and saldo_final is not None and corrente == saldo_final
    # transferência enviada e devolvida (mesmo ID da operação) se anulam
    por_id = {}
    for l in linhas:
        por_id.setdefault(l["id"], []).append(l)
    for grupo in por_id.values():
        if len(grupo) == 2 and grupo[0]["valor"] == grupo[1]["valor"] and grupo[0]["sinal"] != grupo[1]["sinal"]:
            for l in grupo:
                l["ignorar"] = "Transferência enviada e devolvida: se anulam, não é receita nem despesa."
    return {"banco": "Mercado Pago", "conta": conta, "titular": titular, "ini": ini, "fim": fim,
            "saldo_ant": saldo_ant, "saldo_final": saldo_final, "confere": confere,
            "linhas": linhas, "retidos": []}


MESES_ABREV = {"JAN": "01", "FEV": "02", "MAR": "03", "ABR": "04", "MAI": "05", "JUN": "06", "JUL": "07",
               "AGO": "08", "SET": "09", "OUT": "10", "NOV": "11", "DEZ": "12"}
RE_NU_DIA = re.compile(r"^\s*(\d\d) ([A-Z]{3}) (\d{4})\s+(Total de (?:entradas|sa[ií]das))\b")
RE_NU_TOT = re.compile(r"^\s*(Total de (?:entradas|sa[ií]das))\s+([+-])\s*([\d.]+,\d\d)\s*$")
RE_NU_ITEM = re.compile(r"^\s{6,}(\S.*?)(?:\s{3,}(\S.*?))?\s{3,}([\d.]+,\d\d)\s*$")


def ler_nubank(txt):
    """Extrato de conta do Nubank (PDF do app). Mesmo formato de saída do ler_sicoob."""
    if "NUBANK" not in txt.upper() and "NU PAGAMENTOS" not in txt.upper():
        raise ErroExtrato("Este PDF não parece um extrato do Nubank.")
    m = re.search(r"(\d\d) DE (\w+) DE (\d{4}) a (\d\d) DE (\w+) DE (\d{4})", txt)
    if not m:
        raise ErroExtrato("Não encontrei o período do extrato.")
    nomes = {"JANEIRO": "01", "FEVEREIRO": "02", "MARÇO": "03", "ABRIL": "04", "MAIO": "05", "JUNHO": "06",
             "JULHO": "07", "AGOSTO": "08", "SETEMBRO": "09", "OUTUBRO": "10", "NOVEMBRO": "11", "DEZEMBRO": "12"}
    try:
        ini = f"{m.group(3)}-{nomes[m.group(2).upper()]}-{m.group(1)}"
        fim = f"{m.group(6)}-{nomes[m.group(5).upper()]}-{m.group(4)}"
    except KeyError:
        raise ErroExtrato("Não consegui entender o período do extrato.")
    cab = txt.split("Movimentações", 1)[0]
    titular = next((x.strip() for x in cab.split("\n") if x.strip()), "")
    conta = re.search(r"Conta\s*\n?\s*([\d-]+)", cab)
    conta = (conta.group(1) if conta else "")
    def num(rot):
        r = re.search(rot + r"\s+([+-]?)\s*([\d.]+,\d\d)", cab)
        return None if not r else (-_cents(r.group(2)) if r.group(1) == "-" else _cents(r.group(2)))
    saldo_ant, saldo_final = num("Saldo inicial"), num(r"Saldo final do per[ií]odo")
    tot_ent, tot_sai = num("Total de entradas"), num(r"Total de sa[ií]das")
    corpo = txt.split("Movimentações", 1)[1]
    linhas, dia, sinal, ult, saldos = [], None, None, None, []
    pular = False                              # rodapé de uma página + cabeçalho da seguinte
    for pg in [corpo]:
        for lin in pg.split("\n"):
            if re.search(r"Tem alguma d[uú]vida|O saldo l[ií]quido corresponde", lin):
                pular = True
                continue
            if "VALORES EM R$" in lin:
                pular = False
                continue
            if pular or not lin.strip():
                continue
            d = RE_NU_DIA.match(lin)
            if d:
                dia = f"{d.group(3)}-{MESES_ABREV.get(d.group(2), '00')}-{d.group(1)}"
                sinal = "C" if "entradas" in d.group(4) else "D"
                ult = None
                continue
            if RE_NU_TOT.match(lin):
                sinal = "C" if "entradas" in lin else "D"
                ult = None
                continue
            sd = re.match(r"^\s*Saldo do dia\s+(-?[\d.]+,\d\d)\s*$", lin)
            if sd:
                saldos.append((dia, _cents_s(sd.group(1))))
                ult = None
                continue
            it = RE_NU_ITEM.match(lin)
            if it and dia and sinal:
                tipo, desc, val = it.groups()
                ult = {"data": dia, "hist": re.sub(r"\s+", " ", tipo).strip(), "det": [], "valor": _cents(val),
                       "sinal": sinal, "_desc": [re.sub(r"\s+", " ", desc).strip()] if desc else []}
                linhas.append(ult)
                continue
            if ult is not None and lin.strip():
                ult["_desc"].append(re.sub(r"\s+", " ", lin).strip())
    if not linhas:
        raise ErroExtrato("Não encontrei movimentações no extrato.")
    for l in linhas:
        full = " ".join(x for x in l.pop("_desc") if x)
        nome = re.split(r"\s-\s(?:[•\d.\-/* ]+)\s-\s", full, 1)[0].strip()
        l["nu"] = True
        l["pj"] = bool(re.search(r"\d{2}\.\d{3}\.\d{3}\s?/\s?\d{4}", full))
        l["det"] = [x for x in (nome[:80], full) if x]
    ent = sum(l["valor"] for l in linhas if l["sinal"] == "C")
    sai = sum(l["valor"] for l in linhas if l["sinal"] == "D")
    confere = (saldo_ant is not None and saldo_final is not None and saldo_ant + ent - sai == saldo_final
               and (tot_ent is None or tot_ent == ent) and (tot_sai is None or abs(tot_sai) == sai))
    return {"banco": "Nubank", "conta": conta, "titular": titular, "ini": ini, "fim": fim,
            "saldo_ant": saldo_ant, "saldo_final": saldo_final, "confere": confere,
            "linhas": linhas, "retidos": []}


def ler_extrato(txt):
    u = txt.upper()
    if "MERCADO PAGO" in u and "DETALHE DOS MOVIMENTOS" in u:
        return ler_mercadopago(txt)
    if "SICOOB" in u and "HISTÓRICO DE MOVIMENTAÇÃO" in u:
        return ler_sicoob(txt)
    if "NUBANK" in u or ("NU PAGAMENTOS" in u and "SALDO DO DIA" in u):
        return ler_nubank(txt)
    raise ErroExtrato("Não reconheci este PDF. Por enquanto leio os extratos do Sicoob (conta corrente), "
                      "Mercado Pago e Nubank.")


def contraparte(det):
    """Nome de quem pagou/recebeu (ou o convênio), para sugerir a categoria e lembrar da regra."""
    for d in det:
        dl = d.lower()
        if dl.startswith("doc.:"):
            nome = d[5:].strip()
            if nome and not re.fullmatch(r"[\d.\s]+|pix", nome, re.I):
                return nome
            continue
        if dl.startswith(GENERICAS) or re.fullmatch(r"[\d*.\-/\s]+", d) or dl.startswith(("n0077", "codigo")):
            continue
        return re.sub(r"^(FAV|REM)\.:\s*", "", d, flags=re.I)[:80]
    return ""


def chave_regra(l):
    return (l["hist"] + "|" + contraparte(l["det"])).lower()[:150]


MARKET_CARTAO = ("CR CMP", "CR CPS", "CR ANT", "CRÉD.TED", "CRED.TED", "SIPAG")


def cat_garantir(db, nome, tipo, grupo=None):
    r = db.execute("SELECT id FROM banco_categorias WHERE lower(nome)=lower(?) AND tipo=?", (nome, tipo)).fetchone()
    if r:
        return r["id"]
    return db.execute("INSERT INTO banco_categorias(nome,tipo) VALUES(?,?)", (nome, tipo)).lastrowid


def sugerir(db, l, titular, regras, vendas_livres, usadas):
    """(acao, categoria_id, venda_id, nota). 'decidir' = o sistema não arrisca."""
    h, det = l["hist"].upper(), " ".join(l["det"])
    if l.get("ignorar"):
        return "ignorar", None, None, l["ignorar"]
    if l.get("nu"):                                      # Nubank
        nome = re.sub(r"[^A-Z ]", "", (l["det"][0] if l["det"] else "").upper()).split()
        meu = re.sub(r"[^A-Z ]", "", (titular or "").upper()).split()
        if meu and nome == meu and "PIX" in h:
            return "ignorar", None, None, "Transferência para você mesmo: não é receita nem despesa."
        r = regras.get(chave_regra(l))
        if r:
            return r["acao"], r["categoria_id"], None, "Classificado como você fez antes."
        if l["sinal"] == "C":
            if "AMAZON" in det.upper():
                return "receita", cat_garantir(db, "Vendas em marketplace/cartão", "receita"), None, "Repasse de marketplace."
            return "receita", cat_garantir(db, "Recebimentos Pix", "receita"), None, "Entrada do extrato."
        if h.startswith("PAGAMENTO DE BOLETO"):
            return "despesa", cat_garantir(db, "Boletos pagos", "despesa"), None, "Boleto pago."
        if h.startswith("PAGAMENTO DE FATURA"):
            return "despesa", cat_garantir(db, "Fatura do cartão", "despesa"), None, "Pagamento da fatura do cartão."
        if l.get("pj"):
            return "despesa", cat_garantir(db, "Pix a empresas", "despesa"), None, "Pix para empresa/fornecedor."
        return "despesa", cat_garantir(db, "Pix a pessoas", "despesa"), None, \
            "Pix para pessoa física (pode ser retirada dos sócios ou transferência sua: ajuste se quiser)."
    if "id" in l:                                        # Mercado Pago
        if h.startswith(("DINHEIRO RESERVADO", "DINHEIRO RETIRADO")):
            return "ignorar", None, None, "Movimento entre o saldo e a reserva (caixinha) do próprio Mercado Pago."
        r = regras.get(chave_regra(l))
        if r:
            return r["acao"], r["categoria_id"], None, "Classificado como você fez antes."
        if l["sinal"] == "C":
            if h.startswith("LIBERAÇÃO DE DINHEIRO") or h.startswith("LIBERACAO DE DINHEIRO"):
                return "receita", cat_garantir(db, "Vendas em marketplace/cartão", "receita"), None, "Liberação de venda."
            return "receita", cat_garantir(db, "Recebimentos Pix", "receita"), None, "Entrada do extrato."
        if h.startswith("DÉBITO POR DÍVIDA") or h.startswith("DEBITO POR DIVIDA"):
            return "despesa", cat_garantir(db, "Faturas do marketplace", "despesa"), None, "Fatura vencida descontada do saldo."
        return "despesa", cat_garantir(db, "Saídas bancárias", "despesa"), None, "Saída do extrato: ajuste a categoria se quiser."
    if titular and titular.lower() in det.lower() and h.startswith(("TRANSF", "PIX")):
        return "ignorar", None, None, "Transferência entre contas suas: não é receita nem despesa."
    r = regras.get(chave_regra(l))
    if r:
        return r["acao"], r["categoria_id"], None, "Classificado como você fez antes."
    if l["sinal"] == "C":
        if h.startswith("CRED.PROM"):
            return "receita", cat_garantir(db, "Outras receitas", "receita", "outras"), None, "Cashback."
        if h.startswith(MARKET_CARTAO) or "SIPAG" in det.upper():
            return "receita", cat_garantir(db, "Vendas em marketplace/cartão", "receita", "operacional"), None, \
                "Crédito de marketplace/cartão."
        return "receita", cat_garantir(db, "Entradas bancárias", "receita", "outras"), None, "Entrada do extrato."
    if h.startswith(("TARIFA", "DEB PACOTE")):
        return "despesa", cat_garantir(db, "Tarifas bancárias", "despesa", "operacional"), None, ""
    if "TELECOM" in h:
        return "despesa", cat_garantir(db, "Telefone", "despesa", "operacional"), None, ""
    if h.startswith(("DÉB.EMPR", "DEB.EMPR")):
        return "despesa", cat_garantir(db, "Empréstimos e financiamentos", "despesa", "outras"), None, \
            "Parcela de empréstimo (fica em 'outras despesas', fora das despesas operacionais)."
    return "despesa", cat_garantir(db, "Saídas bancárias", "despesa", "operacional"), None, "Saída do extrato: ajuste a categoria se quiser."


def _mes_anterior(ano, mes):
    return f"{ano - 1}-12" if mes == 1 else f"{ano}-{mes - 1:02d}"


def _mes_seguinte(ano, mes):
    return f"{ano + 1}-01" if mes == 12 else f"{ano}-{mes + 1:02d}"


def _saldo_cents(txt):
    t = (txt or "").strip().replace("R$", "").replace(" ", "")
    neg = t.startswith("-")
    t = t.lstrip("+-")
    if not re.fullmatch(r"\d{1,3}(\.\d{3})*(,\d{1,2})?|\d+(,\d{1,2})?|\d+\.\d{1,2}", t):
        raise ValueError
    if "," not in t and re.fullmatch(r"\d+\.\d{1,2}", t):
        t = t.replace(".", ",")
    if "," in t:
        inteiro, dec = t.split(",")
        t = inteiro + "," + dec.ljust(2, "0")
    else:
        t += ",00"
    v = _cents(t)
    return -v if neg else v


def saldos_do_mes(db, ano, mes, banco=""):
    """Por banco: saldo inicial (fechamento informado do mês anterior ou saldo do extrato), movimentação do mês,
    saldo calculado e saldo informado no fechamento deste mês."""
    px, ant = periodo(ano, mes), _mes_anterior(ano, mes)
    bancos = [r[0] for r in db.execute("SELECT DISTINCT banco FROM extratos ORDER BY banco")]
    if banco:
        bancos = [b for b in bancos if b == banco]
    res = []
    for b in bancos:
        mov = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN l.acao='receita' THEN l.valor END),0) ent, "
            "COALESCE(SUM(CASE WHEN l.acao='despesa' THEN l.valor END),0) sai, "
            "COALESCE(SUM(CASE WHEN l.acao='ignorar' THEN CASE WHEN l.sinal='C' THEN l.valor ELSE -l.valor END END),0) ign "
            "FROM extrato_linhas l JOIN extratos e ON e.id=l.extrato_id "
            "WHERE e.banco=? AND l.aplicado=1 AND l.acao IN ('receita','despesa','ignorar') AND substr(l.data,1,7)=?",
            (b, px)).fetchone()
        r = db.execute("SELECT saldo FROM banco_saldos WHERE banco=? AND mes=?", (b, ant)).fetchone()
        inicial, origem = (r["saldo"], f"fechamento de {ant[5:]}/{ant[:4]}") if r else (None, "")
        if inicial is None:
            e = db.execute("SELECT saldo_ant FROM extratos WHERE banco=? AND substr(ini,1,7)=? AND saldo_ant IS NOT NULL "
                           "ORDER BY ini LIMIT 1", (b, px)).fetchone()
            if e:
                inicial, origem = e["saldo_ant"], "extrato importado"
        calc = None if inicial is None else inicial + mov["ent"] - mov["sai"] + mov["ign"]
        inf = db.execute("SELECT saldo FROM banco_saldos WHERE banco=? AND mes=?", (b, px)).fetchone()
        inf = inf["saldo"] if inf else None
        res.append({"banco": b, "inicial": inicial, "origem": origem, "ent": mov["ent"], "sai": mov["sai"],
                    "ign": mov["ign"], "calc": calc, "inf": inf,
                    "dif": None if (inf is None or calc is None) else inf - calc})
    return res


@app.route("/bancos/saldo", methods=["POST"])
def bancos_saldo():
    db = get_db()
    banco, mes_txt = request.form.get("banco", ""), request.form.get("mes", "")
    volta = url_for("bancos", ano=mes_txt[:4], mes=int(mes_txt[5:7]) if re.fullmatch(r"\d{4}-\d\d", mes_txt) else 0,
                    banco=request.form.get("filtro_banco", "")) + "#saldos"
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes_txt) or not db.execute(
            "SELECT 1 FROM extratos WHERE banco=?", (banco,)).fetchone():
        flash("Banco ou mês inválido.", "erro")
        return redirect(url_for("bancos"))
    txt = request.form.get("saldo", "").strip()
    if not txt:
        db.execute("DELETE FROM banco_saldos WHERE banco=? AND mes=?", (banco, mes_txt))
        flash("Saldo do fechamento removido.", "ok")
    else:
        try:
            v = _saldo_cents(txt)
        except ValueError:
            flash("Informe o saldo com números (ex.: 1.234,56).", "erro")
            return redirect(volta)
        db.execute("INSERT INTO banco_saldos(banco,mes,saldo,atualizado_em) VALUES(?,?,?,?) "
                   "ON CONFLICT(banco,mes) DO UPDATE SET saldo=excluded.saldo, atualizado_em=excluded.atualizado_em",
                   (banco, mes_txt, v, agora_txt()))
        flash(f"Saldo de {banco} em {mes_txt[5:]}/{mes_txt[:4]} salvo: {brl(v)}. "
              f"Ele é o saldo inicial de {_mes_seguinte(int(mes_txt[:4]), int(mes_txt[5:]))[5:]}/"
              f"{_mes_seguinte(int(mes_txt[:4]), int(mes_txt[5:]))[:4]}.", "ok")
    db.commit()
    return redirect(volta)


@app.route("/bancos")
def bancos():
    """Movimentação dos extratos importados. Fica separada de vendas, Financeiro e DRE de propósito."""
    db = get_db()
    if request.args.get("ano") is None and request.args.get("mes") is None:
        ult = db.execute("SELECT MAX(l.data) FROM extrato_linhas l WHERE l.aplicado=1 "
                         "AND l.acao IN ('receita','despesa')").fetchone()[0]
        ano, mes = (int(ult[:4]), int(ult[5:7])) if ult else ler_periodo(request.args)
    else:
        ano, mes = ler_periodo(request.args)
    px = periodo(ano, mes)
    banco = request.args.get("banco", "")
    cond = "l.aplicado=1 AND l.acao IN ('receita','despesa') AND substr(l.data,1,?)=?"
    par = [len(px), px]
    if banco:
        cond += " AND e.banco=?"; par.append(banco)
    cat_f = request.args.get("cat", "")
    cat_nome = None
    base = ("FROM extrato_linhas l JOIN extratos e ON e.id=l.extrato_id "
            "LEFT JOIN banco_categorias c ON c.id=l.categoria_id WHERE " + cond)
    tot = db.execute("SELECT COALESCE(SUM(CASE WHEN l.acao='receita' THEN l.valor END),0) ent, "
                     "COALESCE(SUM(CASE WHEN l.acao='despesa' THEN l.valor END),0) sai, COUNT(*) n " + base, par).fetchone()
    por_banco = db.execute("SELECT e.banco, COALESCE(SUM(CASE WHEN l.acao='receita' THEN l.valor END),0) ent, "
                           "COALESCE(SUM(CASE WHEN l.acao='despesa' THEN l.valor END),0) sai, COUNT(*) n " + base +
                           " GROUP BY e.banco ORDER BY e.banco", par).fetchall()
    por_cat = db.execute("SELECT COALESCE(c.id,0) cid, COALESCE(c.nome,'Sem categoria') cat, l.acao, SUM(l.valor) t, "
                         "COUNT(*) n " + base + " GROUP BY cid, cat, l.acao ORDER BY l.acao DESC, t DESC", par).fetchall()
    cond_m, par_m = "", []
    if cat_f.isdigit():
        cond_m = " AND COALESCE(l.categoria_id,0)=?"; par_m = [int(cat_f)]
        r = db.execute("SELECT nome FROM banco_categorias WHERE id=?", (int(cat_f),)).fetchone()
        cat_nome = r["nome"] if r else "Sem categoria"
    movs = db.execute("SELECT l.*, e.banco, e.conta, c.nome cat " + base + cond_m + " ORDER BY l.data DESC, l.id DESC",
                      par + par_m).fetchall()
    sub_ent = sum(m["valor"] for m in movs if m["acao"] == "receita")
    sub_sai = sum(m["valor"] for m in movs if m["acao"] == "despesa")
    lista_bancos = [r[0] for r in db.execute("SELECT DISTINCT banco FROM extratos ORDER BY banco")]
    saldos = saldos_do_mes(db, ano, mes, banco) if mes else []
    extratos = db.execute("SELECT e.*, (SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id AND l.aplicado=1) ok, "
                          "(SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id AND l.aplicado=0 "
                          "AND l.acao NOT IN ('duplicada','retido')) pend FROM extratos e ORDER BY e.id DESC LIMIT 12").fetchall()
    return render_template("bancos.html", titulo="Bancos", ano=ano, mes=mes, meses=MESES, banco=banco, tot=tot,
                           por_banco=por_banco, por_cat=por_cat, movs=movs, cat_f=cat_f, cat_nome=cat_nome,
                           sub_ent=sub_ent, sub_sai=sub_sai, lista_bancos=lista_bancos,
                           extratos=extratos, contraparte=contraparte, saldos=saldos,
                           mes_ant=_mes_anterior(ano, mes) if mes else "", mes_seg=_mes_seguinte(ano, mes) if mes else "")


def _cat_uso(db):
    return {r["cid"]: r["n"] for r in db.execute(
        "SELECT categoria_id cid, COUNT(*) n FROM extrato_linhas WHERE categoria_id IS NOT NULL GROUP BY categoria_id")}


@app.route("/bancos/categorias")
def bancos_categorias():
    db = get_db()
    cats = db.execute("SELECT * FROM banco_categorias WHERE ativo=1 ORDER BY tipo DESC, lower(nome)").fetchall()
    return render_template("bancos_categorias.html", titulo="Categorias dos bancos", cats=cats, uso=_cat_uso(db))


@app.route("/bancos/categorias/nova", methods=["POST"])
def bancos_categoria_nova():
    db = get_db()
    nome = re.sub(r"\s+", " ", request.form.get("nome", "")).strip()[:60]
    tipo = request.form.get("tipo")
    if not nome or tipo not in ("receita", "despesa"):
        flash("Informe o nome e se é entrada ou saída.", "erro")
    elif db.execute("SELECT 1 FROM banco_categorias WHERE lower(nome)=lower(?) AND tipo=?", (nome, tipo)).fetchone():
        flash("Já existe uma categoria com esse nome.", "erro")
    else:
        db.execute("INSERT INTO banco_categorias(nome,tipo) VALUES(?,?)", (nome, tipo)); db.commit()
        flash(f"Categoria “{nome}” criada.", "ok")
    return redirect(url_for("bancos_categorias"))


@app.route("/bancos/categorias/<int:cid>/renomear", methods=["POST"])
def bancos_categoria_renomear(cid):
    db = get_db()
    c = db.execute("SELECT * FROM banco_categorias WHERE id=?", (cid,)).fetchone()
    nome = re.sub(r"\s+", " ", request.form.get("nome", "")).strip()[:60]
    if not c or not nome:
        flash("Informe o nome.", "erro")
    elif db.execute("SELECT 1 FROM banco_categorias WHERE lower(nome)=lower(?) AND tipo=? AND id<>?",
                    (nome, c["tipo"], cid)).fetchone():
        flash("Já existe uma categoria com esse nome.", "erro")
    else:
        db.execute("UPDATE banco_categorias SET nome=? WHERE id=?", (nome, cid)); db.commit()
        flash("Nome alterado.", "ok")
    return redirect(url_for("bancos_categorias"))


@app.route("/bancos/categorias/<int:cid>/excluir", methods=["POST"])
def bancos_categoria_excluir(cid):
    """Exclui a categoria; os movimentos dela vão para a categoria escolhida (ou ficam 'Sem categoria')."""
    db = get_db()
    c = db.execute("SELECT * FROM banco_categorias WHERE id=?", (cid,)).fetchone()
    if not c:
        return redirect(url_for("bancos_categorias"))
    destino = request.form.get("destino", "")
    dest = None
    if destino.isdigit() and int(destino) != cid:
        dest = db.execute("SELECT id FROM banco_categorias WHERE id=? AND tipo=?", (int(destino), c["tipo"])).fetchone()
        dest = dest["id"] if dest else None
    n = _cat_uso(db).get(cid, 0)
    db.execute("UPDATE extrato_linhas SET categoria_id=? WHERE categoria_id=?", (dest, cid))
    if dest:
        db.execute("UPDATE OR IGNORE extrato_regras SET categoria_id=? WHERE categoria_id=?", (dest, cid))
    db.execute("DELETE FROM extrato_regras WHERE categoria_id=?", (cid,))
    db.execute("DELETE FROM banco_categorias WHERE id=?", (cid,))
    db.commit()
    flash(f"Categoria “{c['nome']}” excluída" + (f"; {n} movimento(s) ficaram {'na categoria escolhida' if dest else 'sem categoria'}." if n else "."), "ok")
    return redirect(url_for("bancos_categorias"))



@app.route("/bancos/extrato")
def extrato_envio():
    db = get_db()
    return render_template("extrato_envio.html", titulo="Importar extrato", rows=db.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id AND l.aplicado=1) ok, "
        "(SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id) total "
        "FROM extratos e ORDER BY e.id DESC LIMIT 30").fetchall())


@app.route("/bancos/extrato/enviar", methods=["POST"])
def extrato_enviar():
    db = get_db()
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        flash("Escolha o arquivo PDF do extrato.", "erro")
        return redirect(url_for("extrato_envio"))
    dados = arq.read(8 * 1024 * 1024 + 1)
    if len(dados) > 8 * 1024 * 1024:
        flash("O PDF passa de 8 MB.", "erro")
        return redirect(url_for("extrato_envio"))
    h = hashlib.sha256(dados).hexdigest()
    ja = db.execute("SELECT id FROM extratos WHERE hash=?", (h,)).fetchone()
    if ja:
        flash("Este arquivo já foi enviado antes; abri a conferência dele.", "aviso")
        return redirect(url_for("extrato_ver", eid=ja["id"]))
    try:
        ex = ler_extrato(texto_pdf(dados))
    except ErroExtrato as e:
        flash(str(e), "erro")
        return redirect(url_for("extrato_envio"))
    regras = {r["chave"]: r for r in db.execute("SELECT * FROM extrato_regras")}
    livres = []
    cur = db.execute("INSERT INTO extratos(banco,conta,titular,ini,fim,arquivo,hash,saldo_ant,saldo_final,"
                     "confere,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (ex["banco"], ex["conta"], ex["titular"], ex["ini"], ex["fim"],
                      (arq.filename or "")[:200], h, ex["saldo_ant"], ex["saldo_final"],
                      1 if ex["confere"] else 0, agora_txt()))
    eid = cur.lastrowid
    vistos, usadas = {}, set()
    for l in ex["linhas"]:
        base = "|".join((ex["banco"], ex["conta"], l["data"], l["hist"], " ".join(l["det"]) + l.get("id", ""),
                         str(l["valor"]), l["sinal"]))
        vistos[base] = vistos.get(base, 0) + 1       # lançamentos idênticos no mesmo dia têm número de ordem
        chave = hashlib.sha1(f"{base}|{vistos[base]}".encode()).hexdigest()
        dup = db.execute("SELECT 1 FROM extrato_linhas WHERE chave=? AND aplicado=1", (chave,)).fetchone()
        if dup:
            acao, cat, vid, nota = "duplicada", None, None, "Já importada em outro extrato."
        else:
            acao, cat, vid, nota = sugerir(db, l, ex["titular"], regras, livres, usadas)
        db.execute("INSERT INTO extrato_linhas(extrato_id,chave,data,historico,detalhe,valor,sinal,acao,"
                   "categoria_id,venda_id,nota) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (eid, chave, l["data"], l["hist"], "\n".join(l["det"]), l["valor"], l["sinal"],
                    acao, cat, vid, nota))
    for l in ex["retidos"]:
        db.execute("INSERT INTO extrato_linhas(extrato_id,chave,data,historico,detalhe,valor,sinal,acao,nota) "
                   "VALUES(?,?,?,?,?,?,?,'retido',?)",
                   (eid, "ret|" + h + "|" + l["data"] + l["hist"], l["data"], l["hist"], "\n".join(l["det"]),
                    l["valor"], "*", "Valor retido: só conta no dia em que for liberado."))
    db.commit()
    return redirect(url_for("extrato_ver", eid=eid))


def _extrato_ctx(db, eid):
    e = db.execute("SELECT * FROM extratos WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    lin = db.execute("SELECT * FROM extrato_linhas WHERE extrato_id=? ORDER BY data, id", (eid,)).fetchall()
    return e, lin


@app.route("/bancos/extrato/<int:eid>")
def extrato_ver(eid):
    db = get_db()
    e, lin = _extrato_ctx(db, eid)
    cats = [dict(r) for r in db.execute("SELECT id,nome,tipo FROM banco_categorias WHERE ativo=1 ORDER BY nome")]
    ent = sum(l["valor"] for l in lin if l["sinal"] == "C" and l["acao"] != "duplicada")
    sai = sum(l["valor"] for l in lin if l["sinal"] == "D" and l["acao"] != "duplicada")
    pend = sum(1 for l in lin if not l["aplicado"] and l["acao"] in ("decidir",))
    return render_template("extrato_ver.html", titulo="Conferir extrato", e=e, lin=lin,
                           cats=cats, ent=ent, sai=sai, pend=pend,
                           contraparte=contraparte)


@app.route("/bancos/extrato/<int:eid>/confirmar", methods=["POST"])
def extrato_confirmar(eid):
    db = get_db()
    e, lin = _extrato_ctx(db, eid)
    f = request.form
    erros, plano, usadas = [], [], set()
    for l in lin:
        if l["aplicado"] or l["acao"] in ("duplicada", "retido"):
            continue
        acao = f.get(f"acao_{l['id']}", "decidir")
        if acao == "decidir":
            db.execute("UPDATE extrato_linhas SET acao='decidir' WHERE id=?", (l["id"],))
            continue
        cat = inteiro(f.get(f"cat_{l['id']}"), 0) or None
        vid = inteiro(f.get(f"venda_{l['id']}"), 0) or None
        quem = f"{dia(l['data'])} {l['historico']}"
        if acao in ("receita", "despesa"):
            if (acao == "receita") != (l["sinal"] == "C"):
                erros.append(f"{quem}: entrada só pode ser receita e saída só despesa.")
                continue
            if not db.execute("SELECT 1 FROM banco_categorias WHERE id=? AND tipo=?", (cat, acao)).fetchone():
                erros.append(f"{quem}: escolha a categoria.")
                continue
        elif acao != "ignorar":
            erros.append(f"{quem}: ação inválida.")
            continue
        plano.append((l, acao, cat, vid))
    if erros:
        for x in erros[:8]:
            flash(x, "erro")
        return redirect(url_for("extrato_ver", eid=eid))
    try:
        for l, acao, cat, vid in plano:
            db.execute("UPDATE extrato_linhas SET acao=?, categoria_id=?, aplicado=1 WHERE id=?",
                       (acao, cat if acao != "ignorar" else None, l["id"]))
            if acao in ("receita", "despesa", "ignorar"):
                db.execute("INSERT OR REPLACE INTO extrato_regras(chave,acao,categoria_id) VALUES(?,?,?)",
                           (chave_regra({"hist": l["historico"], "det": (l["detalhe"] or "").split("\n")}),
                            acao, cat))
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Falha ao importar extrato")
        flash("Não foi possível importar. Nada foi lançado.", "erro")
        return redirect(url_for("extrato_ver", eid=eid))
    flash(f"{len(plano)} linha(s) importada(s).", "ok")
    return redirect(url_for("extrato_ver", eid=eid))


@app.route("/bancos/extrato/<int:eid>/desfazer", methods=["POST"])
def extrato_desfazer(eid):
    """Desfaz tudo que esta importação lançou (as linhas voltam para 'decidir')."""
    db = get_db()
    e, lin = _extrato_ctx(db, eid)
    for l in lin:
        if not l["aplicado"]:
            continue
        db.execute("UPDATE extrato_linhas SET aplicado=0 WHERE id=?", (l["id"],))
    db.commit()
    flash("Importação desfeita: as linhas saíram da página Bancos e voltaram para conferência.", "ok")
    return redirect(url_for("extrato_ver", eid=eid))


@app.route("/bancos/extrato/<int:eid>/excluir", methods=["POST"])
def extrato_excluir(eid):
    db = get_db()
    e, lin = _extrato_ctx(db, eid)
    if any(l["aplicado"] for l in lin):
        flash("Este extrato já lançou itens. Desfaça a importação antes de excluir.", "erro")
        return redirect(url_for("extrato_ver", eid=eid))
    db.execute("DELETE FROM extrato_linhas WHERE extrato_id=?", (eid,))
    db.execute("DELETE FROM extratos WHERE id=?", (eid,))
    db.commit()
    flash("Extrato removido.", "ok")
    return redirect(url_for("extrato_envio"))


# -------------------------------- Início ---------------------------------
@app.route("/")
def dashboard():
    db = get_db()
    mes = datetime.now().strftime("%Y-%m")
    v = db.execute("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM vendas "
                   "WHERE status='ativa' AND substr(data,1,7)=?", (mes,)).fetchone()
    ab = db.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM orcamentos "
                    "WHERE status='aberto'").fetchone()
    ap = db.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM orcamentos "
                    "WHERE status IN ('aprovado','producao','concluido') AND substr(aprovado_em,1,7)=?",
                    (mes,)).fetchone()
    abertos = db.execute("SELECT o.*, c.nome cliente FROM orcamentos o JOIN clientes c "
                         "ON c.id=o.cliente_id WHERE o.status='aberto' "
                         "ORDER BY o.id DESC LIMIT 6").fetchall()
    agora = datetime.now().strftime("%Y-%m-%dT%H:%M")
    baixo = db.execute("SELECT * FROM produtos WHERE ativo=1 AND servico=0 "
                       "AND estoque<=estoque_min ORDER BY estoque LIMIT 8").fetchall()
    prox = db.execute("SELECT a.*, c.nome cliente FROM agenda a LEFT JOIN clientes c "
                      "ON c.id=a.cliente_id WHERE a.status='agendado' AND a.data_hora>=? "
                      "ORDER BY a.data_hora LIMIT 6", (agora,)).fetchall()
    atrasados = db.execute("SELECT COUNT(*) FROM agenda WHERE status='agendado' AND data_hora<?",
                           (agora,)).fetchone()[0]
    ultimas = db.execute("SELECT v.*, c.nome cliente FROM vendas v LEFT JOIN clientes c "
                         "ON c.id=v.cliente_id ORDER BY v.id DESC LIMIT 6").fetchall()
    n_clientes = db.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
    investido = db.execute("SELECT COALESCE(SUM(custo * CASE WHEN estoque>0 THEN estoque ELSE 0 END),0) "
                           "FROM produtos WHERE ativo=1 AND servico=0").fetchone()[0]
    return render_template("dashboard.html", titulo="Início", v=v, ab=ab, ap=ap, investido=investido,
                           abertos=abertos, baixo=baixo, prox=prox, ultimas=ultimas,
                           n_clientes=n_clientes, atrasados=atrasados)


# -------------------------------- Clientes -------------------------------
@app.route("/clientes")
def clientes():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        rows = db.execute("SELECT * FROM clientes WHERE nome LIKE ? OR telefone LIKE ? "
                          "OR email LIKE ? ORDER BY nome", (f"%{q}%",) * 3).fetchall()
    else:
        rows = db.execute("SELECT * FROM clientes ORDER BY nome").fetchall()
    return render_template("clientes.html", titulo="Clientes", rows=rows, q=q)


@app.route("/clientes/<int:cid>")
def cliente_ver(cid):
    db = get_db()
    c = db.execute("SELECT * FROM clientes WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    orcs = db.execute("SELECT * FROM orcamentos WHERE cliente_id=? ORDER BY id DESC",
                      (cid,)).fetchall()
    linhas = db.execute(
        "SELECT l.*, o.id oid, o.data odata, o.status ostatus, o.titulo otitulo "
        "FROM orc_linhas l JOIN orcamentos o ON o.id=l.orcamento_id "
        "WHERE o.cliente_id=? ORDER BY o.id DESC, l.ordem, l.id", (cid,)).fetchall()
    comps = defaultdict(list)
    for r in db.execute("SELECT i.* FROM orcamento_itens i JOIN orcamentos o "
                        "ON o.id=i.orcamento_id WHERE o.cliente_id=? ORDER BY i.id", (cid,)):
        comps[r["linha_id"]].append(r)
    vendas_c = db.execute("SELECT * FROM vendas WHERE cliente_id=? ORDER BY id DESC",
                          (cid,)).fetchall()
    fechado = sum(o["total"] for o in orcs if o["status"] in ("aprovado", "concluido"))
    return render_template("cliente_ver.html", titulo=c["nome"], c=c, orcs=orcs,
                           linhas=linhas, comps=comps, vendas=vendas_c, fechado=fechado)


@app.route("/clientes/novo", methods=["GET", "POST"])
@app.route("/clientes/<int:cid>/editar", methods=["GET", "POST"])
def cliente_form(cid=None):
    db = get_db()
    c = db.execute("SELECT * FROM clientes WHERE id=?", (cid,)).fetchone() if cid else None
    if cid and not c:
        abort(404)
    if request.method == "POST":
        f = request.form
        nome = f.get("nome", "").strip()
        if not nome:
            flash("Informe o nome do cliente.", "erro")
            return render_template("cliente_form.html", titulo="Cliente", c=f, cid=cid)
        tel = f.get("telefone", "").strip()
        norm = norm_tel(tel)
        dados = (nome, tel, f.get("email", "").strip(),
                 f.get("obs", "").strip(), f.get("cidade", "").strip(),
                 f.get("endereco", "").strip(), norm)

        def telefone_repetido():
            if not norm or (c and norm_tel(c["telefone"]) == norm):   # o próprio cliente pode manter o seu
                return None
            return db.execute("SELECT id, nome FROM clientes WHERE telefone_norm=? AND id<>?",
                              (norm, cid or -1)).fetchone()
        outro = telefone_repetido()
        if outro:
            flash(MSG_TEL_DUP + f" (cliente: {outro['nome']})", "erro")
            return render_template("cliente_form.html", titulo="Cliente", c=f, cid=cid), 400
        try:
            if cid:
                db.execute("UPDATE clientes SET nome=?,telefone=?,email=?,obs=?,cidade=?,endereco=?,"
                           "telefone_norm=? WHERE id=?", dados + (cid,))
            else:
                cur = db.execute("INSERT INTO clientes(nome,telefone,email,obs,cidade,endereco,"
                                 "telefone_norm,criado_em) VALUES(?,?,?,?,?,?,?,?)",
                                 dados + (datetime.now().isoformat(" ", "seconds"),))
                cid = cur.lastrowid
            db.commit()
        except sqlite3.IntegrityError:      # gravação simultânea: a restrição do banco barra
            db.rollback()
            flash(MSG_TEL_DUP, "erro")
            return render_template("cliente_form.html", titulo="Cliente", c=f, cid=cid), 400
        flash("Cliente salvo.", "ok")
        return redirect(url_for("cliente_ver", cid=cid))
    return render_template("cliente_form.html", titulo="Cliente", c=c, cid=cid)


@app.route("/clientes/<int:cid>/excluir", methods=["POST"])
def cliente_excluir(cid):
    db = get_db()
    if (db.execute("SELECT 1 FROM vendas WHERE cliente_id=?", (cid,)).fetchone()
            or db.execute("SELECT 1 FROM orcamentos WHERE cliente_id=?", (cid,)).fetchone()):
        flash("Este cliente tem vendas ou orçamentos registrados e não pode ser excluído.", "erro")
    else:
        db.execute("DELETE FROM agenda WHERE cliente_id=?", (cid,))
        db.execute("DELETE FROM clientes WHERE id=?", (cid,))
        db.commit()
        flash("Cliente excluído.", "ok")
    return redirect(url_for("clientes"))


# -------------------------------- Produtos -------------------------------
def cats_existentes(db):
    """Categorias cadastradas (o formulário mostra as ativas e a atual do produto, se inativa)."""
    return db.execute("SELECT id, nome, ativo FROM categorias_produto ORDER BY lower(nome)").fetchall()


MSG_CAT_DUP = "Já existe uma categoria com esse nome."
MSG_SKU_DUP = "Este SKU já está cadastrado em outro produto. Informe um código diferente."
MSG_TEL_DUP = "Este telefone já está cadastrado para outro cliente. Verifique o cadastro existente."


def nome_categoria(db, nome, ignorar=None):
    """Valida o nome. Devolve (nome_limpo, erro)."""
    nome = re.sub(r"\s+", " ", (nome or "").strip())
    if not nome:
        return None, "Informe o nome da categoria."
    if len(nome) > 60:
        return None, "O nome da categoria pode ter no máximo 60 caracteres."
    if db.execute("SELECT 1 FROM categorias_produto WHERE lower(trim(nome))=lower(?) AND id<>?",
                  (nome, ignorar or -1)).fetchone():
        return None, MSG_CAT_DUP
    return nome, None


@app.route("/api/categorias-produto", methods=["GET", "POST"])
def api_categorias_produto():
    db = get_db()
    if request.method == "POST":
        nome, erro = nome_categoria(db, (request.get_json(silent=True) or {}).get("nome"))
        if erro:
            return jsonify(ok=False, erro=erro), 400
        try:
            cur = db.execute("INSERT INTO categorias_produto(nome) VALUES(?)", (nome,))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify(ok=False, erro=MSG_CAT_DUP), 400
        return jsonify(ok=True, id=cur.lastrowid, nome=nome)
    return jsonify(categorias=[dict(r) for r in db.execute(
        "SELECT id, nome FROM categorias_produto WHERE ativo=1 ORDER BY lower(nome)")])


@app.route("/produtos/categorias")
def categorias_produto():
    db = get_db()
    rows = db.execute("SELECT c.*, (SELECT COUNT(*) FROM produtos p WHERE p.categoria_id=c.id) usos "
                      "FROM categorias_produto c ORDER BY lower(c.nome)").fetchall()
    return render_template("categorias.html", titulo="Categorias de produtos", rows=rows)


@app.route("/produtos/categorias/nova", methods=["POST"])
def categoria_nova():
    db = get_db()
    nome, erro = nome_categoria(db, request.form.get("nome"))
    if erro:
        flash(erro, "erro")
    else:
        try:
            db.execute("INSERT INTO categorias_produto(nome) VALUES(?)", (nome,))
            db.commit()
            flash("Categoria cadastrada.", "ok")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(MSG_CAT_DUP, "erro")
    return redirect(url_for("categorias_produto"))


@app.route("/produtos/categorias/<int:cid>/renomear", methods=["POST"])
def categoria_renomear(cid):
    db = get_db()
    if not db.execute("SELECT 1 FROM categorias_produto WHERE id=?", (cid,)).fetchone():
        abort(404)
    nome, erro = nome_categoria(db, request.form.get("nome"), cid)
    if erro:
        flash(erro, "erro")
    else:
        try:
            db.execute("UPDATE categorias_produto SET nome=? WHERE id=?", (nome, cid))
            db.execute("UPDATE produtos SET categoria=? WHERE categoria_id=?", (nome, cid))
            db.commit()
            flash("Categoria renomeada; os produtos dela acompanharam o novo nome.", "ok")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(MSG_CAT_DUP, "erro")
    return redirect(url_for("categorias_produto"))


@app.route("/produtos/categorias/<int:cid>/ativar", methods=["POST"])
def categoria_ativar(cid):
    db = get_db()
    c = db.execute("SELECT ativo FROM categorias_produto WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    db.execute("UPDATE categorias_produto SET ativo=? WHERE id=?", (0 if c["ativo"] else 1, cid))
    db.commit()
    flash("Categoria desativada: some da lista de novos cadastros e os produtos atuais não mudam."
          if c["ativo"] else "Categoria ativada.", "ok")
    return redirect(url_for("categorias_produto"))


@app.route("/produtos/categorias/<int:cid>/reclassificar", methods=["POST"])
def categoria_reclassificar(cid):
    """Operação explícita: move todos os produtos desta categoria para outra."""
    db = get_db()
    dest = db.execute("SELECT * FROM categorias_produto WHERE id=?",
                      (inteiro(request.form.get("destino"), 0),)).fetchone()
    if not dest or dest["id"] == cid:
        flash("Escolha a categoria de destino.", "erro")
    else:
        n = db.execute("UPDATE produtos SET categoria_id=?, categoria=? WHERE categoria_id=?",
                       (dest["id"], dest["nome"], cid)).rowcount
        db.commit()
        flash(f"{n} produto(s) reclassificado(s) para {dest['nome']}.", "ok")
    return redirect(url_for("categorias_produto"))


@app.route("/produtos/categorias/<int:cid>/excluir", methods=["POST"])
def categoria_excluir(cid):
    db = get_db()
    if db.execute("SELECT 1 FROM produtos WHERE categoria_id=?", (cid,)).fetchone():
        flash("Esta categoria é usada por produtos e não pode ser excluída. Reclassifique os "
              "produtos para outra categoria ou apenas desative esta.", "erro")
    else:
        db.execute("DELETE FROM categorias_produto WHERE id=?", (cid,))
        db.commit()
        flash("Categoria excluída.", "ok")
    return redirect(url_for("categorias_produto"))


@app.route("/duplicidades")
def duplicidades():
    """Relatório para corrigir à mão duplicidades antigas (nada é apagado nem mesclado sozinho)."""
    db = get_db()
    skus = []
    for r in db.execute("SELECT lower(trim(sku)) k FROM produtos WHERE trim(COALESCE(sku,''))<>'' "
                        "GROUP BY k HAVING COUNT(*)>1"):
        skus.append((r["k"], db.execute("SELECT id, nome, sku, ativo FROM produtos "
                                        "WHERE lower(trim(sku))=? ORDER BY id", (r["k"],)).fetchall()))
    tels = []
    for r in db.execute("SELECT telefone_norm k FROM clientes WHERE telefone_norm IS NOT NULL "
                        "AND telefone_norm<>'' GROUP BY k HAVING COUNT(*)>1"):
        tels.append((r["k"], db.execute("SELECT id, nome, telefone FROM clientes WHERE telefone_norm=? "
                                        "ORDER BY id", (r["k"],)).fetchall()))
    idx = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    return render_template("duplicidades.html", titulo="Duplicidades", skus=skus, tels=tels,
                           idx_sku="idx_prod_sku" in idx, idx_tel="idx_cli_tel" in idx)


@app.route("/produtos")
def produtos():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        rows = db.execute("SELECT * FROM produtos WHERE ativo=1 AND (nome LIKE ? OR sku LIKE ?) "
                          "ORDER BY nome", (f"%{q}%",) * 2).fetchall()
    else:
        rows = db.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    # os custos NÃO são enviados nesta página: só vêm de /api/produtos/custos ao clicar em "Mostrar custos"
    return render_template("produtos.html", titulo="Produtos", rows=rows, q=q)


@app.route("/api/produtos/custos")
def api_custos():
    """Preços de custo e totais financeiros do estoque (só leitura, nada é alterado)."""
    db = get_db()
    todos = [r for r in db.execute("SELECT * FROM produtos WHERE ativo=1") if controla_estoque(r)]
    valor = sum(r["preco"] * max(r["estoque"], 0) for r in todos)
    investido = sum(r["custo"] * max(r["estoque"], 0) for r in todos)
    sem_custo = sum(1 for r in todos if r["estoque"] > 0 and not r["custo"])
    prods = {}
    for r in db.execute("SELECT * FROM produtos WHERE ativo=1"):
        custo = brl(r["custo"]) + (f" / {UNIDADES[r['unidade']]}" if r["unidade"] != "un" else "") \
            if r["custo"] else "—"
        inv = brl(r["custo"] * max(r["estoque"], 0)) if (controla_estoque(r) and r["custo"]) else "—"
        prods[r["id"]] = {"custo": custo, "investido": inv}
    resp = jsonify(produtos=prods, sem_custo=sem_custo,
                   kpi={"investido": brl(investido), "valor": brl(valor),
                        "lucro": brl(valor - investido)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/produtos/novo", methods=["GET", "POST"])
@app.route("/produtos/<int:pid>/editar", methods=["GET", "POST"])
def produto_form(pid=None):
    db = get_db()
    p = db.execute("SELECT * FROM produtos WHERE id=?", (pid,)).fetchone() if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        nome = f.get("nome", "").strip()
        try:
            preco = centavos(f.get("preco"))
        except ValueError:
            preco = -1
        if not nome or preco < 0:
            flash("Informe o nome e um preço válido (ex.: 25,90).", "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db))
        unidade = f.get("unidade") if f.get("unidade") in UNIDADES else "un"
        servico = 1 if f.get("servico") else 0
        ctrl = not servico
        try:
            est = round(decimal_opc(f.get("estoque")) or 0.0, 4)
            est_min = round(decimal_opc(f.get("estoque_min")) or 0.0, 3)
        except ValueError:
            flash("Informe o estoque com números (ex.: 12,5).", "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db))
        try:
            custo = centavos(f.get("custo") or "0")
            if custo < 0:
                raise ValueError
        except ValueError:
            flash("Informe o preço de custo com números (ex.: 12,50).", "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db))
        try:
            esp = decimal_opc(f.get("espessura")) if unidade == "ml" else None
            if esp is not None and esp < 0:
                raise ValueError
        except ValueError:
            flash("Informe a espessura do perfil em centímetros (ex.: 3).", "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db))
        try:
            minimo = centavos(f.get("minimo")) if (f.get("minimo") or "").strip() else None
            if minimo is not None and minimo < 0:
                raise ValueError
        except ValueError:
            flash("Informe o valor mínimo de cobrança com números (ex.: 15,00) ou deixe em branco.", "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db))
        sku = f.get("sku", "").strip()
        if not sku and not pid:
            sku = proximo_sku(db)                  # SKU em branco no cadastro novo: usa o contador padrão
        atual = db.execute("SELECT sku, categoria_id FROM produtos WHERE id=?", (pid,)).fetchone() if pid else None
        erro = None
        if sku and not (atual and (atual["sku"] or "").strip().lower() == sku.lower()) and db.execute(
                "SELECT 1 FROM produtos WHERE lower(trim(sku))=lower(?) AND id<>?", (sku, pid or -1)).fetchone():
            erro = MSG_SKU_DUP
        cat_id = inteiro(f.get("categoria_id"), 0) or None
        cat_nome = ""
        if cat_id and not erro:
            c = db.execute("SELECT * FROM categorias_produto WHERE id=?", (cat_id,)).fetchone()
            if not c or (not c["ativo"] and not (atual and atual["categoria_id"] == cat_id)):
                erro = "Escolha uma categoria cadastrada e ativa."
            else:
                cat_nome = c["nome"]
        if erro:
            flash(erro, "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db)), 400
        dados = (nome, sku, preco, est if ctrl else 0,
                 est_min if ctrl else 0, unidade, servico, custo,
                 cat_nome, cat_id, esp, minimo)
        try:
            if pid:
                db.execute("UPDATE produtos SET nome=?,sku=?,preco=?,estoque=?,estoque_min=?,"
                           "unidade=?,servico=?,custo=?,categoria=?,categoria_id=?,espessura=?,minimo=? "
                           "WHERE id=?", dados + (pid,))
            else:
                db.execute("INSERT INTO produtos(nome,sku,preco,estoque,estoque_min,unidade,servico,"
                           "custo,categoria,categoria_id,espessura,minimo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           dados)
            db.commit()
        except sqlite3.IntegrityError:      # duas gravações ao mesmo tempo: a restrição do banco barra
            db.rollback()
            flash(MSG_SKU_DUP, "erro")
            return render_template("produto_form.html", titulo="Produto", p=f, pid=pid,
                                   cats=cats_existentes(db)), 400
        flash("Produto salvo.", "ok")
        return redirect(url_for("produtos"))
    return render_template("produto_form.html", titulo="Produto", p=p, pid=pid,
                           cats=cats_existentes(db), sku_sug=None if pid else proximo_sku(db))


def proximo_sku(db):
    """Próximo código do contador padrão (0001, 0002...). Só contam SKUs formados apenas por números (até 6
    dígitos); códigos próprios como PRT3-PT e códigos de barras longos não mexem no contador."""
    maior = 0
    for r in db.execute("SELECT trim(sku) k FROM produtos WHERE trim(COALESCE(sku,''))<>''"):
        if re.fullmatch(r"\d{1,6}", r["k"]):
            maior = max(maior, int(r["k"]))
    n = maior + 1
    while db.execute("SELECT 1 FROM produtos WHERE lower(trim(sku))=?", (f"{n:04d}",)).fetchone():
        n += 1
    return f"{n:04d}"


@app.route("/produtos/<int:pid>/excluir", methods=["POST"])
def produto_excluir(pid):
    db = get_db()
    if (db.execute("SELECT 1 FROM itens_venda WHERE produto_id=?", (pid,)).fetchone()
            or db.execute("SELECT 1 FROM orcamento_itens WHERE produto_id=?", (pid,)).fetchone()):
        db.execute("UPDATE produtos SET ativo=0 WHERE id=?", (pid,))
        flash("Produto já foi usado em venda/orçamento, então foi arquivado "
              "(some da lista, o histórico fica).", "ok")
    else:
        db.execute("DELETE FROM produtos WHERE id=?", (pid,))
        flash("Produto excluído.", "ok")
    db.commit()
    return redirect(url_for("produtos"))


@app.route("/produtos/<int:pid>/movimentos")
def produto_movimentos(pid):
    db = get_db()
    p = db.execute("SELECT * FROM produtos WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    movs = db.execute("SELECT * FROM mov_estoque WHERE produto_id=? ORDER BY id DESC LIMIT 300",
                      (pid,)).fetchall()
    return render_template("produto_mov.html", titulo="Histórico de estoque", p=p, movs=movs)


# ------------------------------- Orçamentos ------------------------------
# Um orçamento tem vários "produtos" (ex.: 2 quadros 25×30 cm). Cada produto tem
# suas próprias medidas, quantidade e componentes (moldura, vidro, fundo...).
# Todos os cálculos acontecem aqui, no servidor, com Decimal (sem erro de centavos).
# A tela só pede o resultado para /api/orcamentos/calcular e mostra.
ORDEM_CATEGORIAS = ("moldura", "vidro", "fundo", "duratex", "espelho", "paspatur")  # só ordena a tela

def lista_produtos_json(db, somente_un=False):
    sql = "SELECT * FROM produtos WHERE ativo=1 "
    if somente_un:
        sql += "AND unidade='un' "
    rows = db.execute(sql + "ORDER BY nome").fetchall()
    return [{"id": r["id"], "nome": r["nome"], "un": r["unidade"], "preco": r["preco"],
             "servico": r["servico"], "estoque": r["estoque"],
             "controla": 1 if controla_estoque(r) else 0} for r in rows]


def catalogo_categorias(db):
    """Materiais ativos agrupados por categoria. Tudo vem do cadastro de produtos."""
    grupos = {}
    for r in db.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome"):
        nome = (r["categoria"] or "").strip() or "Outros"
        g = grupos.setdefault(nome.lower(), {"nome": nome, "produtos": []})
        g["produtos"].append({"id": r["id"], "nome": r["nome"], "un": r["unidade"],
                              "preco": r["preco"], "estoque": r["estoque"],
                              "controla": 0 if r["servico"] else 1, "minimo": r["minimo"] or 0,
                              "espessura": num_f(r["espessura"])})

    def ordem(g):
        k = g["nome"].lower()
        if k in ORDEM_CATEGORIAS:
            return (0, ORDEM_CATEGORIAS.index(k), k)
        return (2 if k == "outros" else 1, 0, k)
    return sorted(grupos.values(), key=ordem)


def lista_clientes(db):
    return [{"id": r["id"], "nome": r["nome"], "telefone": r["telefone"] or "",
             "cidade": r["cidade"] or "", "endereco": r["endereco"] or ""}
            for r in db.execute("SELECT * FROM clientes ORDER BY nome")]


@app.route("/api/clientes")
def api_clientes():
    return jsonify(lista_clientes(get_db()))


def view_comp(categoria, descricao, unidade, qtd_unit, preco_unit, sub_unit, pecas,
              minimo=None, calc_unit=None, **extra):
    """Dados de um componente prontos para a tela (textos já formatados)."""
    rot = UNIDADES.get(unidade, unidade)
    q = fmt_qtd(qtd_unit, unidade)
    v = {"categoria": categoria or "", "descricao": descricao, "unidade": unidade,
         "rotulo_un": rot, "qtd_unit": float(qtd_unit), "qtd_unit_txt": q,
         "qtd_total": float(Decimal(str(qtd_unit)) * pecas),
         "preco_unit": preco_unit, "preco_unit_txt": brl(preco_unit),
         "sub_unit": sub_unit, "sub_unit_txt": brl(sub_unit),
         "subtotal": sub_unit * pecas, "subtotal_txt": brl(sub_unit * pecas),
         "tag": f"{categoria or descricao} — {q} {rot}",
         "minimo": minimo, "minimo_txt": brl(minimo) if minimo else "", "calc_unit": calc_unit,
         "calc_unit_txt": brl(calc_unit) if calc_unit is not None else "",
         "minimo_aplicado": bool(minimo and calc_unit is not None and calc_unit < minimo),
         "piso": (minimo or 0) * pecas}
    v.update(extra)
    return v


def view_linha(nome, descricao, largura, altura, pecas, perda, comps, valor_unit):
    medidas = f"{num_f(largura)} × {num_f(altura)} cm" if largura and altura else ""
    return {"nome": nome, "descricao": descricao, "largura": largura, "altura": altura,
            "medidas": medidas, "qtd": pecas, "perda": perda, "componentes": comps,
            "valor_unit": valor_unit, "valor_unit_txt": brl(valor_unit),
            "piso": sum(c.get("piso", 0) for c in comps),
            "total": valor_unit * pecas, "total_txt": brl(valor_unit * pecas)}


def _calcular(db, d):
    """Motor de cálculo de UM produto do orçamento.

    Regras (cm nas medidas, m e m² nos cálculos):
      m²  : (largura × altura) / 10.000            × preço do m²
      ML  : (2 × (largura + altura) + perda) / 100 × preço do metro linear
      un  : quantidade por peça                    × preço unitário
    Primeiro fecha o valor de UMA peça completa (cada componente arredondado
    em centavos); depois multiplica pela quantidade de peças.
    Devolve (resultado_para_a_tela, dados_para_gravar).
    """
    erros, avisos = [], []
    nome = str(d.get("nome") or "").strip()[:120]
    if not nome:
        erros.append("Informe o nome do produto / serviço.")
    descricao = str(d.get("descricao") or "").strip()[:500]

    def numero(campo, rotulo):
        try:
            return dec(d.get(campo))
        except ValueError:
            erros.append(f"{rotulo}: informe um número válido.")
            return None

    L, A = numero("largura", "Largura"), numero("altura", "Altura")
    for v, r in ((L, "Largura"), (A, "Altura")):
        if v is not None and v <= 0:
            erros.append(f"{r} deve ser maior que zero.")
    # Medidas de PRECIFICAÇÃO: largura e altura sobem ao próximo múltiplo de 5 cm. As medidas reais
    # continuam sendo as mostradas ao cliente. Orçamentos antigos reabertos (arred=false) não mudam.
    arred5 = str(d.get("arred", True)).lower() not in ("false", "0", "none", "")
    Lc = multiplo5(L) if (arred5 and L is not None and L > 0) else L
    Ac = multiplo5(A) if (arred5 and A is not None and A > 0) else A
    perda = numero("perda", "Perda de moldura") or D0
    if perda < 0:
        erros.append("A perda de moldura não pode ser negativa.")
        perda = D0
    try:
        q = dec(d.get("qtd"))
        if q is None or q != q.to_integral_value() or q < 1:
            raise ValueError
        pecas = int(q)
    except ValueError:
        pecas = 1
        erros.append("A quantidade de peças deve ser um número inteiro maior que zero.")

    prods = {r["id"]: r for r in db.execute("SELECT * FROM produtos")}
    views, raws = [], []
    for c in d.get("componentes") or []:
        if str(c.get("ativo", True)).lower() in ("false", "0", "none", ""):
            continue                                   # desativado: fora do cálculo
        pid = inteiro(c.get("produto_id"), 0) or None
        prod = prods.get(pid) if pid else None
        cat = str(c.get("categoria") or "").strip()
        if pid and not prod:
            erros.append("Um material usado não existe mais no cadastro.")
            continue
        if prod:
            unidade, desc_mat = prod["unidade"], prod["nome"]
            controla = not prod["servico"]
            estoque = prod["estoque"]
            cat = cat or (prod["categoria"] or "").strip()
        else:
            if not c.get("custom"):
                erros.append(f"Selecione o material de {cat or 'um componente'}.")
                continue
            unidade, desc_mat = c.get("unidade"), str(c.get("nome") or "").strip()[:120]
            controla, estoque = False, None
            if not desc_mat:
                erros.append("Dê um nome ao componente avulso.")
                continue
            if unidade not in UNIDADES:
                erros.append(f"{desc_mat}: escolha a unidade (un, m² ou ML).")
                continue
        rotulo = cat or desc_mat

        try:
            esp = dec(c.get("espessura"))               # informativo (não entra no cálculo)
            if esp is not None and esp < 0:
                esp = None
        except ValueError:
            esp = None
        l_ef, a_ef = Lc, Ac    # medidas do PRODUTO (arredondadas) são a única fonte dos cálculos

        manual = str(c.get("manual", False)).lower() in ("true", "1")
        if manual:
            try:
                qu = dec(c.get("qtd"))
            except ValueError:
                qu = None
            if qu is None or qu <= 0:
                erros.append(f"{rotulo}: informe uma quantidade maior que zero.")
                continue
        elif unidade in ("m2", "ml"):
            if not l_ef or not a_ef or l_ef <= 0 or a_ef <= 0:
                erros.append(f"{rotulo}: informe largura e altura maiores que zero.")
                continue
            if unidade == "m2":
                qu = l_ef * a_ef / 10000
            else:
                qu = (2 * (l_ef + a_ef) + perda) / 100
        else:  # unidade
            try:
                qu = dec(c.get("qtd"))
            except ValueError:
                qu = None
            qu = Decimal(1) if qu is None and not c.get("qtd") else qu
            if qu is None or qu <= 0:
                erros.append(f"{rotulo}: informe uma quantidade maior que zero.")
                continue
        qu = arred(qu, 6)
        if qu <= 0:
            erros.append(f"{rotulo}: a quantidade calculada ficou zerada.")
            continue

        try:
            preco = para_centavos(c.get("preco"))
        except ValueError:
            erros.append(f"{rotulo}: preço inválido.")
            continue
        if preco is None:
            preco = prod["preco"] if prod else None
            if preco is None:
                erros.append(f"{rotulo}: informe o preço.")
                continue
        if preco < 0:
            erros.append(f"{rotulo}: o preço não pode ser negativo.")
            continue

        calc = int(arred(qu * preco))                       # quantidade × tarifa, por peça
        try:
            if "minimo" in c:                               # valor guardado no orçamento
                minimo = para_centavos(c.get("minimo"))
            else:                                           # sem informação: usa o cadastro
                minimo = prod["minimo"] if prod else None
            if minimo is not None and minimo < 0:
                raise ValueError
        except ValueError:
            erros.append(f"{rotulo}: valor mínimo inválido.")
            continue
        sub_unit = max(calc, minimo or 0)                   # piso: o MAIOR dos dois, nunca soma
        perda_ap = perda if (unidade == "ml" and not manual) else D0
        v = view_comp(cat, desc_mat, unidade, qu, preco, sub_unit, pecas, minimo=minimo,
                      calc_unit=calc, key=c.get("key"), produto_id=pid)
        if controla and estoque is not None and float(qu * pecas) > estoque + 0.0005:
            avisos.append(f"Estoque insuficiente de {desc_mat} (saldo {num_f(estoque, 4)} "
                          f"{UNIDADES[unidade]}). Isso não impede o orçamento.")
        views.append(v)
        raws.append({"produto_id": pid, "descricao": desc_mat, "unidade": unidade,
                     "larg": float(l_ef) if l_ef else None, "alt": float(a_ef) if a_ef else None,
                     "pecas": pecas, "qtd": float(qu * pecas), "preco_unit": preco,
                     "subtotal": sub_unit * pecas, "categoria": cat, "qtd_unit": float(qu),
                     "sub_unit": sub_unit, "minimo": minimo, "calc_unit": calc, "larg_ov": None, "alt_ov": None,
                     "manual": 1 if manual else 0,
                     "espessura": float(esp) if esp is not None else None,
                     "perda": float(perda_ap)})
    if not views:
        erros.append("Ative pelo menos um acompanhamento (moldura, vidro, fundo, serviço...).")
    valor_unit = sum(v["sub_unit"] for v in views)
    largura = float(L) if L else None
    altura = float(A) if A else None
    res = {"ok": not erros, "erros": erros, "avisos": avisos,
           "linha": view_linha(nome, descricao, largura, altura, pecas, float(perda), views,
                               valor_unit)}
    base = ""
    if arred5 and L and A and L > 0 and A > 0 and (Lc != L or Ac != A):
        base = f"{num_f(Lc)} × {num_f(Ac)} cm"          # só para a tela interna
    res["linha"]["base_calc"] = base
    raw = {"arred5": 1 if arred5 else 0, "largura_calc": float(Lc) if Lc else None,
           "altura_calc": float(Ac) if Ac else None,
           "nome": nome, "descricao": descricao, "largura": largura, "altura": altura,
           "qtd": pecas, "perda": float(perda), "valor_unit": valor_unit,
           "total": valor_unit * pecas, "comps": raws}
    return res, raw


@app.route("/api/orcamentos/calcular", methods=["POST"])
def api_calcular():
    d = request.get_json(force=True, silent=True)
    return jsonify(_calcular(get_db(), d if isinstance(d, dict) else {})[0])


def gravar_linhas(db, oid, raws):
    for ordem, r in enumerate(raws, 1):
        cur = db.execute(
            "INSERT INTO orc_linhas(orcamento_id,ordem,nome,descricao,largura,altura,qtd,perda,"
            "valor_unit,total,largura_calc,altura_calc,arred5) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, ordem, r["nome"], r["descricao"], r["largura"], r["altura"], r["qtd"],
             r["perda"], r["valor_unit"], r["total"], r["largura_calc"], r["altura_calc"],
             r["arred5"]))
        lid = cur.lastrowid
        for c in r["comps"]:
            db.execute(
                "INSERT INTO orcamento_itens(orcamento_id,produto_id,descricao,unidade,larg,alt,"
                "pecas,qtd,preco_unit,subtotal,linha_id,categoria,qtd_unit,sub_unit,larg_ov,"
                "alt_ov,manual,espessura,perda,minimo,calc_unit,desc_rateio) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, c["produto_id"], c["descricao"], c["unidade"], c["larg"], c["alt"],
                 c["pecas"], c["qtd"], c["preco_unit"], c["subtotal"], lid, c["categoria"],
                 c["qtd_unit"], c["sub_unit"], c["larg_ov"], c["alt_ov"], c["manual"],
                 c["espessura"], c["perda"], c["minimo"], c["calc_unit"],
                 c.get("desc_rateio", 0)))


@app.route("/orcamentos")
def orcamentos():
    q = request.args.get("q", "").strip()
    st = request.args.get("status", "")
    sql = ("SELECT o.*, c.nome cliente FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
           "WHERE 1=1 ")
    par = []
    if q:
        sql += "AND (c.nome LIKE ? OR o.titulo LIKE ? OR CAST(o.id AS TEXT)=? " \
               "OR EXISTS(SELECT 1 FROM orc_linhas l WHERE l.orcamento_id=o.id AND l.nome LIKE ?)) "
        par += [f"%{q}%", f"%{q}%", q.lstrip("#"), f"%{q}%"]
    if st in ORC:
        sql += "AND o.status=? "
        par.append(st)
    rows = get_db().execute(sql + "ORDER BY o.id DESC LIMIT 300", par).fetchall()
    return render_template("orcamentos.html", titulo="Orçamentos", rows=rows, q=q, st=st)


def aplicar_estoque(db, oid, baixar):
    """Desconta (baixar=True) ou devolve (False) ao estoque os itens do orçamento.
    O saldo pode ficar negativo; cada movimentação é registrada em mov_estoque."""
    sinal = -1 if baixar else 1
    tipo = "saida_orcamento" if baixar else "estorno_orcamento"
    for i in db.execute("SELECT oi.produto_id, SUM(oi.qtd) AS qtd FROM orcamento_itens oi "
                        "JOIN produtos p ON p.id=oi.produto_id WHERE oi.orcamento_id=? "
                        "AND p.servico=0 GROUP BY oi.produto_id", (oid,)).fetchall():
        mover_estoque(db, i["produto_id"], sinal * i["qtd"], tipo, orcamento_id=oid)


def consumo_orcamento(db, oid):
    """{produto_id: quantidade} que o orçamento consome do estoque (serviços ficam de fora)."""
    return {r["produto_id"]: r["qtd"] for r in db.execute(
        "SELECT oi.produto_id, SUM(oi.qtd) AS qtd FROM orcamento_itens oi JOIN produtos p "
        "ON p.id=oi.produto_id WHERE oi.orcamento_id=? AND p.servico=0 "
        "GROUP BY oi.produto_id", (oid,))}


def ajustar_estoque(db, oid, antes, depois):
    """Orçamento aprovado e editado: lança só a DIFERENÇA (sem baixar tudo de novo).
    Devolve os produtos cujo saldo ficou negativo por causa do ajuste (aviso)."""
    negativos = []
    for pid in sorted(set(antes) | set(depois)):
        delta = round(depois.get(pid, 0) - antes.get(pid, 0), 4)   # >0 = passou a consumir mais
        if abs(delta) < 0.00005:
            continue
        mover_estoque(db, pid, -delta, "ajuste_orcamento", orcamento_id=oid)
        p = db.execute("SELECT nome, estoque, unidade FROM produtos WHERE id=?", (pid,)).fetchone()
        if delta > 0 and p["estoque"] < -0.0005:
            negativos.append(f"{p['nome']} (saldo {num_f(p['estoque'], 4)} {UNIDADES[p['unidade']]})")
    return "; ".join(negativos)


def ratear_desconto(raws, desconto):
    """Distribui o desconto entre os componentes, proporcionalmente à folga de cada um
    (valor cobrado − valor mínimo × peças), sem nunca levar um componente abaixo do seu mínimo."""
    comps = [c for r in raws for c in r["comps"]]
    folgas = [c["subtotal"] - (c["minimo"] or 0) * c["pecas"] for c in comps]
    total = sum(folgas)
    for c in comps:
        c["desc_rateio"] = 0
    if desconto <= 0 or total <= 0:
        return
    base = [desconto * f // total for f in folgas]
    resto = desconto - sum(base)
    ordem = sorted(range(len(comps)), key=lambda i: (-(desconto * folgas[i] % total), i))
    for i in ordem:
        if resto <= 0:
            break
        if base[i] < folgas[i]:
            base[i] += 1
            resto -= 1
    for c, b in zip(comps, base):
        c["desc_rateio"] = b


def faltas(db, oid):
    """Aviso (só informativo) dos produtos cujo saldo não cobre o orçamento ('' se tudo ok)."""
    rows = db.execute(
        "SELECT p.nome, p.unidade, p.estoque, SUM(oi.qtd) AS usa FROM orcamento_itens oi "
        "JOIN produtos p ON p.id=oi.produto_id WHERE oi.orcamento_id=? AND p.servico=0 "
        "GROUP BY p.id HAVING SUM(oi.qtd) > p.estoque + 0.0005", (oid,)).fetchall()
    return "; ".join(f"{r['nome']} (saldo {num_f(r['estoque'], 4)} → {num_f(r['estoque'] - r['usa'], 4)} "
                     f"{UNIDADES[r['unidade']]})" for r in rows)


def estado_novo(db, cliente_id):
    ultimo = db.execute("SELECT perda FROM orcamentos ORDER BY id DESC LIMIT 1").fetchone()
    return {"oid": None, "token": uuid.uuid4().hex, "cliente_id": inteiro(cliente_id, 0),
            "data": datetime.now().strftime("%Y-%m-%d"), "titulo": "",
            "perda": num_f(ultimo["perda"]) if ultimo and ultimo["perda"] else "0",
            "desconto": "0,00", "desconto_tipo": "valor", "entrega": "",
            "prazo": "", "obs": "", "linhas": [], "baixado": False,
            "vendedor": empresa_dados(db)["vendedor"]}


def estado_orcamento(db, o, cat):
    """Orçamento salvo -> mesmo formato que a tela usa (entradas + valores históricos)."""
    nomes_cat = {g["nome"].lower(): {p["id"] for p in g["produtos"]} for g in cat}
    nome_real = {g["nome"].lower(): g["nome"] for g in cat}
    linhas = []
    for l in db.execute("SELECT * FROM orc_linhas WHERE orcamento_id=? ORDER BY ordem, id",
                        (o["id"],)).fetchall():
        entradas, views, usadas = [], [], set()
        for n, c in enumerate(db.execute("SELECT * FROM orcamento_itens WHERE linha_id=? "
                                         "ORDER BY id", (l["id"],)).fetchall()):
            pid, cat_nome = c["produto_id"], (c["categoria"] or "").strip()
            if pid and not cat_nome:                  # item antigo: usa a categoria do cadastro
                for g in cat:
                    if pid in nomes_cat[g["nome"].lower()]:
                        cat_nome = g["nome"]
                        break
            chave = None
            if pid and cat_nome.lower() in nomes_cat and pid in nomes_cat[cat_nome.lower()] \
                    and cat_nome.lower() not in usadas:
                usadas.add(cat_nome.lower())
                chave = "c:" + nome_real[cat_nome.lower()]
            custom = chave is None
            if custom:
                chave = f"x:{n}"
            # orçamentos antigos com medida própria no componente: guarda a quantidade já
            # calculada (histórico preservado); "↺ automático" volta a usar as medidas do produto
            fixo = bool(c["manual"]) or c["larg_ov"] is not None or c["alt_ov"] is not None
            auto_un = fixo or c["unidade"] == "un"
            entradas.append({
                "key": chave, "custom": custom, "produto_id": pid,
                "categoria": nome_real.get(cat_nome.lower(), cat_nome), "nome": c["descricao"],
                "unidade": c["unidade"], "manual": fixo and c["unidade"] != "un",
                "qtd": num_f(c["qtd_unit"], 6) if auto_un else "",
                "preco": dinheiro(c["preco_unit"]), "espessura": num_f(c["espessura"]),
                "minimo": dinheiro(c["minimo"]) if c["minimo"] else ""})
            views.append(view_comp(cat_nome, c["descricao"], c["unidade"], c["qtd_unit"],
                                   c["preco_unit"], c["sub_unit"], l["qtd"],
                                   minimo=c["minimo"], calc_unit=c["calc_unit"], key=chave,
                                   produto_id=pid))
        linhas.append({
            "inputs": {"arred": bool(l["arred5"]), "nome": l["nome"], "descricao": l["descricao"] or "",
                       "largura": num_f(l["largura"]), "altura": num_f(l["altura"]),
                       "qtd": l["qtd"], "perda": num_f(l["perda"]) or "0",
                       "componentes": entradas},
            "view": view_linha(l["nome"], l["descricao"] or "", l["largura"], l["altura"],
                               l["qtd"], l["perda"], views, l["valor_unit"])})
    return {"oid": o["id"], "token": None, "cliente_id": o["cliente_id"],
            "data": (o["data"] or "")[:10], "titulo": o["titulo"] or "",
            "perda": num_f(o["perda"]) or "0",
            "desconto_tipo": "pct" if o["desconto_tipo"] == "pct" else "valor",
            "desconto": (num_f(o["desconto_valor"], 2) or "0") if o["desconto_tipo"] == "pct"
            else dinheiro(o["desconto"]),
            "entrega": (o["entrega"] or "")[:10],
            "prazo": o["prazo"] or "", "obs": o["obs"] or "", "linhas": linhas,
            "baixado": bool(o["estoque_baixado"]), "vendedor": o["vendedor"] or ""}


@app.route("/orcamentos/novo")
@app.route("/orcamentos/<int:oid>/editar")
def orcamento_form(oid=None):
    db = get_db()
    o = db.execute("SELECT * FROM orcamentos WHERE id=?", (oid,)).fetchone() if oid else None
    if oid and not o:
        abort(404)
    cat = catalogo_categorias(db)
    estado = estado_orcamento(db, o, cat) if o else estado_novo(db, request.args.get("cliente_id"))
    nomes = [r[0] for r in db.execute(
        "SELECT nome FROM orc_linhas GROUP BY nome ORDER BY COUNT(*) DESC, nome LIMIT 40")]
    nomes += [r[0] for r in db.execute("SELECT nome FROM produtos WHERE ativo=1 AND servico=1")
              if r[0] not in nomes]
    return render_template("orcamento_form.html", titulo="Orçamento", oid=oid, cat=cat,
                           clientes=lista_clientes(db), estado=estado, nomes=nomes)


def data_orcamento(txt, atual=None):
    txt = (txt or "").strip()
    if not txt:
        return atual or datetime.now().isoformat(" ", "seconds")
    datetime.strptime(txt, "%Y-%m-%d")                      # ValueError se inválida
    if atual and atual[:10] == txt:
        return atual                                        # mantém a hora original
    return f"{txt} {datetime.now():%H:%M:%S}"


@app.route("/api/orcamentos/salvar", methods=["POST"])
def api_salvar():
    """Valida e recalcula TUDO no servidor antes de gravar."""
    d = request.get_json(force=True, silent=True)
    d = d if isinstance(d, dict) else {}
    db = get_db()
    oid = inteiro(d.get("oid"), 0) or None
    erros = []
    o = None
    if oid:
        o = db.execute("SELECT * FROM orcamentos WHERE id=?", (oid,)).fetchone()
        if not o:
            return jsonify(ok=False, erros=["Orçamento não encontrado."]), 404

    cid = inteiro(d.get("cliente_id"), 0)
    if not cid or not db.execute("SELECT 1 FROM clientes WHERE id=?", (cid,)).fetchone():
        erros.append("Escolha o cliente do orçamento.")
    try:
        data = data_orcamento(d.get("data"), o["data"] if o else None)
    except ValueError:
        data = None
        erros.append("Data do orçamento inválida.")
    tipo = "pct" if str(d.get("desconto_tipo") or "").lower() in ("pct", "%") else "valor"
    try:                                  # um único tipo de desconto por vez: % ou R$
        dv = dec(d.get("desconto")) or D0
        if dv < 0:
            raise ValueError
        if tipo == "pct":
            dv = arred(dv, 2)
            if dv > 100:
                erros.append("O desconto em porcentagem não pode passar de 100%.")
    except ValueError:
        dv = D0
        erros.append("O desconto deve ser um número válido (ex.: 10 ou 10,50), sem sinal negativo.")
    entrega = str(d.get("entrega") or "").strip()
    if entrega:
        try:
            datetime.strptime(entrega, "%Y-%m-%d")
        except ValueError:
            erros.append("Data prevista de entrega inválida.")
    entrega = entrega or None
    try:
        perda_pad = dec(d.get("perda")) or D0
        if perda_pad < 0:
            raise ValueError
    except ValueError:
        perda_pad = D0
        erros.append("A perda padrão de moldura deve ser um número válido.")

    raws = []
    linhas = d.get("linhas") or []
    if not linhas:
        erros.append("Adicione ao menos um produto ao orçamento.")
    for i, ld in enumerate(linhas, 1):
        res, raw = _calcular(db, ld)
        for e in res["erros"]:
            erros.append(f"Produto {i} ({raw['nome'] or 'sem nome'}): {e}")
        raws.append(raw)
    subtotal = sum(r["total"] for r in raws)
    if tipo == "pct":
        desconto = int(arred(Decimal(subtotal) * dv / 100))      # % sobre o subtotal dos itens
    else:
        desconto = int(arred(dv * 100))                          # R$ fixo
    teto = sum(c["subtotal"] - (c["minimo"] or 0) * c["pecas"] for r in raws for c in r["comps"])
    if tipo != "pct" and desconto > subtotal:
        erros.append("O desconto não pode ser maior que o subtotal dos produtos.")
    elif desconto > teto and not (tipo == "pct" and dv > 100):
        erros.append(f"O desconto de {brl(desconto)} ultrapassa o máximo permitido de {brl(teto)}: "
                     "os valores mínimos de cobrança dos materiais são preservados.")
    total = subtotal - desconto
    if erros:
        return jsonify(ok=False, erros=erros), 400
    ratear_desconto(raws, desconto)

    token = (str(d.get("token") or "")[:64] or None) if not oid else None
    if token:                                               # clique repetido / reenvio
        ja = db.execute("SELECT id FROM orcamentos WHERE token=?", (token,)).fetchone()
        if ja:
            return jsonify(ok=True, url=url_for("orcamento_ver", oid=ja["id"]), repetido=True)

    titulo = str(d.get("titulo") or "").strip()[:200]
    prazo = str(d.get("prazo") or "").strip()[:200]
    obs = str(d.get("obs") or "").strip()[:2000]
    if "vendedor" in d:
        vendedor = str(d.get("vendedor") or "").strip()[:80] or None
    else:                                  # não informado: mantém o do pedido (ou o padrão, se novo)
        vendedor = (o["vendedor"] if o else empresa_dados(db)["vendedor"]) or None
    cab = (cid, titulo, data, float(perda_pad), desconto, total, prazo, obs, tipo, float(dv), entrega,
           vendedor)
    baixado = bool(o and o["estoque_baixado"])
    negativos = ""
    try:                      # tudo ou nada: pedido e estoque nunca ficam pela metade
        antes = consumo_orcamento(db, oid) if (oid and baixado) else {}
        if oid:
            db.execute("UPDATE orcamentos SET cliente_id=?,titulo=?,data=?,perda=?,desconto=?,"
                       "total=?,prazo=?,obs=?,desconto_tipo=?,desconto_valor=?,entrega=?,vendedor=? "
                       "WHERE id=?", cab + (oid,))
            db.execute("DELETE FROM orcamento_itens WHERE orcamento_id=?", (oid,))
            db.execute("DELETE FROM orc_linhas WHERE orcamento_id=?", (oid,))
        else:
            cur = db.execute("INSERT INTO orcamentos(cliente_id,titulo,data,perda,desconto,total,"
                             "prazo,obs,desconto_tipo,desconto_valor,entrega,vendedor,token) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", cab + (token,))
            oid = cur.lastrowid
        gravar_linhas(db, oid, raws)
        if o and o["status"] == "concluido":          # venda já contabilizada: ajusta, não duplica
            atualizar_venda(db, oid)
        if baixado:           # aprovado: só o ajuste (diferença) entra no estoque
            negativos = ajustar_estoque(db, oid, antes, consumo_orcamento(db, oid))
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Falha ao salvar o orçamento")
        return jsonify(ok=False, erros=["Não foi possível salvar. Nada foi alterado, tente de novo."]), 500
    flash("Orçamento salvo.", "ok")
    if negativos:
        flash("Aviso: estoque insuficiente, o saldo ficou negativo em: " + negativos + ".", "aviso")
    return jsonify(ok=True, url=url_for("orcamento_ver", oid=oid))


MSG_AVALIACAO_PADRAO = ("Olá, {nome}! Aqui é da {empresa}. Seu serviço (nº {os}) está concluído e foi um prazer "
                        "atender você. 😊\n\nSe puder, deixe sua avaliação da nossa loja no Google, leva menos de "
                        "1 minuto:\n{link}\n\nMuito obrigado!")


def cfg_get(db, chave, padrao=""):
    r = db.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r and r["valor"] is not None else padrao


def cfg_set(db, chave, valor):
    db.execute("INSERT INTO config(chave,valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
               (chave, valor))


def numero_whatsapp(tel):
    """Número no formato do wa.me (só dígitos com 55) ou None se não parecer um celular/fixo válido."""
    n = norm_tel(tel)
    if not n:
        return None
    if n.startswith("+"):
        d = n[1:]
        return d if 8 <= len(d) <= 15 else None
    return "55" + n if len(n) in (10, 11) else None


def montar_msg_avaliacao(db, o):
    modelo = cfg_get(db, "aval_msg", "") or MSG_AVALIACAO_PADRAO
    nome = (o["cliente"] or "").strip().split(" ")[0].title() or "tudo bem"
    return (modelo.replace("{nome}", nome).replace("{empresa}", empresa_dados(db)["nome"])
            .replace("{os}", f"{o['id']:04d}").replace("{link}", cfg_get(db, "aval_link", "")))


def _orc_aval(db, oid):
    return db.execute("SELECT o.*, c.nome cliente, c.telefone FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
                      "WHERE o.id=? AND o.status='concluido'", (oid,)).fetchone()


@app.route("/avaliacoes")
def avaliacoes():
    db = get_db()
    aba = request.args.get("aba", "pendentes")
    cond = {"pendentes": "AND o.aval_enviada_em IS NULL AND o.aval_dispensada=0",
            "enviadas": "AND o.aval_enviada_em IS NOT NULL",
            "dispensadas": "AND o.aval_dispensada=1 AND o.aval_enviada_em IS NULL"}.get(aba, "")
    rows = db.execute("SELECT o.id, o.titulo, o.concluido_em, o.aval_enviada_em, o.aval_dispensada, c.nome cliente, "
                      "c.telefone FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id WHERE o.status='concluido' "
                      + cond + " ORDER BY COALESCE(o.concluido_em, o.data) DESC, o.id DESC LIMIT 300").fetchall()
    cont = {k: db.execute("SELECT COUNT(*) FROM orcamentos o WHERE o.status='concluido' " + v).fetchone()[0]
            for k, v in (("pendentes", "AND o.aval_enviada_em IS NULL AND o.aval_dispensada=0"),
                         ("enviadas", "AND o.aval_enviada_em IS NOT NULL"),
                         ("dispensadas", "AND o.aval_dispensada=1 AND o.aval_enviada_em IS NULL"), ("todas", ""))}
    link = cfg_get(db, "aval_link", "")
    return render_template("avaliacoes.html", titulo="Avaliações", rows=rows, aba=aba, cont=cont, link=link,
                           modelo=cfg_get(db, "aval_msg", "") or MSG_AVALIACAO_PADRAO, numero_whatsapp=numero_whatsapp)


@app.route("/avaliacoes/config", methods=["POST"])
def avaliacoes_config():
    db = get_db()
    link = request.form.get("link", "").strip()
    msg = request.form.get("msg", "").replace("\r", "").strip()
    if link and not re.match(r"^https?://\S+$", link):
        flash("O link precisa começar com https:// (copie o link de avaliação do seu Perfil da Empresa no Google).", "erro")
        return redirect(url_for("avaliacoes"))
    if len(msg) > 800:
        flash("A mensagem passa de 800 caracteres.", "erro")
        return redirect(url_for("avaliacoes"))
    if msg and "{link}" not in msg:
        msg += "\n{link}"                       # a mensagem sempre leva o link
    cfg_set(db, "aval_link", link)
    cfg_set(db, "aval_msg", "" if msg == MSG_AVALIACAO_PADRAO else msg)
    db.commit()
    flash("Configuração da avaliação salva.", "ok")
    return redirect(url_for("avaliacoes"))


@app.route("/avaliacoes/<int:oid>/enviar", methods=["POST"])
def avaliacao_enviar(oid):
    """Marca como enviada e abre o WhatsApp (celular, WhatsApp Web ou aplicativo) com a mensagem pronta."""
    db = get_db()
    o = _orc_aval(db, oid)
    if not o:
        abort(404)
    if not cfg_get(db, "aval_link", ""):
        flash("Cadastre primeiro o link de avaliação do Google.", "erro")
        return redirect(url_for("avaliacoes"))
    num = numero_whatsapp(o["telefone"])
    if not num:
        flash(f"O cliente {o['cliente']} não tem um telefone válido cadastrado.", "erro")
        return redirect(url_for("avaliacoes"))
    db.execute("UPDATE orcamentos SET aval_enviada_em=?, aval_dispensada=0 WHERE id=?", (agora_txt(), oid))
    db.commit()
    from urllib.parse import quote
    return redirect(f"https://wa.me/{num}?text={quote(montar_msg_avaliacao(db, o))}")


@app.route("/avaliacoes/<int:oid>/estado", methods=["POST"])
def avaliacao_estado(oid):
    db = get_db()
    if not _orc_aval(db, oid):
        abort(404)
    acao = request.form.get("acao")
    if acao == "enviada":
        db.execute("UPDATE orcamentos SET aval_enviada_em=?, aval_dispensada=0 WHERE id=?", (agora_txt(), oid))
    elif acao == "dispensar":
        db.execute("UPDATE orcamentos SET aval_dispensada=1 WHERE id=? AND aval_enviada_em IS NULL", (oid,))
    elif acao == "pendente":
        db.execute("UPDATE orcamentos SET aval_enviada_em=NULL, aval_dispensada=0 WHERE id=?", (oid,))
    db.commit()
    return redirect(request.form.get("volta") or url_for("avaliacoes"))


@app.route("/orcamentos/<int:oid>")
def orcamento_ver(oid):
    db = get_db()
    o = db.execute("SELECT o.*, c.nome cliente, c.telefone, c.email, c.cidade, c.endereco "
                   "FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id WHERE o.id=?",
                   (oid,)).fetchone()
    if not o:
        abort(404)
    linhas = db.execute("SELECT * FROM orc_linhas WHERE orcamento_id=? ORDER BY ordem, id",
                        (oid,)).fetchall()
    comps = defaultdict(list)
    for c in db.execute("SELECT * FROM orcamento_itens WHERE orcamento_id=? ORDER BY id", (oid,)):
        comps[c["linha_id"]].append(c)
    subtotal = sum(l["total"] for l in linhas)
    return render_template("orcamento_ver.html", titulo=f"Orçamento #{oid}", o=o, linhas=linhas,
                           comps=comps, subtotal=subtotal)


@app.route("/orcamentos/<int:oid>/imprimir")
def orcamento_imprimir(oid):
    """Documento de impressão leve: página própria, sem menu/scripts/fontes externas.
    modo: orc (1 via), os (ordem de serviço, 1 via) ou 2vias (cliente + interna na mesma folha A4).
    Só lê o orçamento: imprimir nunca cria nem altera registro."""
    db = get_db()
    o = db.execute("SELECT o.*, c.nome cliente, c.telefone, c.email, c.cidade, c.endereco "
                   "FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id WHERE o.id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    modo = request.args.get("modo", "orc")
    modo = modo if modo in ("orc", "os", "2vias") else "orc"
    linhas = db.execute("SELECT * FROM orc_linhas WHERE orcamento_id=? ORDER BY ordem, id", (oid,)).fetchall()
    comps = defaultdict(list)
    for c in db.execute("SELECT linha_id, categoria, descricao FROM orcamento_itens WHERE orcamento_id=? "
                        "ORDER BY id", (oid,)):
        comps[c["linha_id"]].append(c)
    # cabe em uma folha com as duas vias? (senão cada via vai em uma folha, sem cortar nada)
    peso = 0
    for l in linhas:
        txt = " · ".join((c["categoria"] or c["descricao"] or "") for c in comps[l["id"]])
        peso += 1 + (1 if len(txt) + len(l["nome"] or "") > 75 else 0) + (1 if l["descricao"] else 0)
    cabe = peso <= 7 and len(o["obs"] or "") <= 240
    return render_template("imprimir.html", titulo=f"Orçamento {oid:04d}", o=o, linhas=linhas, comps=comps,
                           subtotal=sum(l["total"] for l in linhas), modo=modo, cabe=cabe,
                           auto=request.args.get("auto") == "1")


# ---- venda gerada pelo orçamento Concluído (uma única vez por orçamento) ----
def agora_txt():
    return datetime.now().isoformat(" ", "seconds")


def evento_venda(db, vid, evento, antes=None, depois=None, obs=None, quando=None):
    db.execute("INSERT INTO venda_eventos(venda_id,data,evento,total_antes,total_depois,obs) "
               "VALUES(?,?,?,?,?,?)", (vid, quando or agora_txt(), evento, antes, depois, obs))


def custos_orcamento(db, oid, venda_id=None):
    """Custo dos materiais do orçamento (CPV) com os preços de custo cadastrados.
    Produto já usado nesta venda mantém o custo histórico guardado. Custo desconhecido NÃO vira
    zero: o item é contado como pendente e o CPV fica 'incompleto'. Serviço sem custo informado
    não é custo de material e fica de fora."""
    antigos = {}
    if venda_id:
        antigos = {r["produto_id"]: r["custo_unit"] for r in db.execute(
            "SELECT produto_id, custo_unit FROM venda_custos WHERE venda_id=? AND conhecido=1 "
            "AND produto_id IS NOT NULL", (venda_id,))}
    itens, cpv, pend = [], 0, 0
    for r in db.execute(
            "SELECT oi.produto_id, oi.descricao, SUM(oi.qtd) AS qtd, p.custo, p.servico "
            "FROM orcamento_itens oi LEFT JOIN produtos p ON p.id=oi.produto_id "
            "WHERE oi.orcamento_id=? GROUP BY oi.produto_id, oi.descricao", (oid,)).fetchall():
        pid = r["produto_id"]
        custo = (antigos.get(pid) or r["custo"]) if pid else None
        if pid and r["servico"] and not custo:
            continue
        if not custo:
            pend += 1
            itens.append((pid, r["descricao"], r["qtd"], None, None, 0))
            continue
        sub = int(arred(Decimal(str(r["qtd"])) * custo))
        cpv += sub
        itens.append((pid, r["descricao"], r["qtd"], custo, sub, 1))
    return itens, cpv, pend


def gravar_custos(db, vid, itens):
    db.execute("DELETE FROM venda_custos WHERE venda_id=?", (vid,))
    db.executemany("INSERT INTO venda_custos(venda_id,produto_id,descricao,qtd,custo_unit,subtotal,"
                   "conhecido) VALUES(?,?,?,?,?,?,?)", [(vid,) + tuple(x) for x in itens])


def registrar_venda(db, oid, quando=None, origem=None):
    """Orçamento Concluído -> venda. Idempotente: concluir de novo não cria nem soma nada."""
    o = db.execute("SELECT * FROM orcamentos WHERE id=?", (oid,)).fetchone()
    v = db.execute("SELECT * FROM vendas WHERE orcamento_id=?", (oid,)).fetchone()
    if not o or (v and v["status"] == "ativa"):
        return None
    quando = quando or agora_txt()
    itens, cpv, pend = custos_orcamento(db, oid, v["id"] if v else None)
    bruto = o["total"] + o["desconto"]
    if v:                                             # estornada antes: reativa a mesma venda
        vid = v["id"]
        db.execute("UPDATE vendas SET status='ativa', cliente_id=?, data=?, total=?, valor_bruto=?,"
                   "desconto=?, cpv=?, cpv_pendentes=?, recebido_em=NULL WHERE id=?",
                   (o["cliente_id"], quando, o["total"], bruto, o["desconto"], cpv, pend, vid))
        evento_venda(db, vid, "reativada", 0, o["total"], "orçamento concluído de novo", quando)
        resultado = "reativada"
    else:
        cur = db.execute(
            "INSERT INTO vendas(cliente_id,data,total,status,obs,orcamento_id,valor_bruto,desconto,"
            "cpv,cpv_pendentes) VALUES(?,?,?,'ativa',?,?,?,?,?,?)",
            (o["cliente_id"], quando, o["total"], f"Orçamento #{oid}", oid, bruto, o["desconto"],
             cpv, pend))
        vid = cur.lastrowid
        evento_venda(db, vid, "concluida", None, o["total"], origem, quando)
        resultado = "criada"
    gravar_custos(db, vid, itens)
    return resultado


def atualizar_venda(db, oid):
    """Orçamento concluído foi editado: atualiza a MESMA venda e registra o ajuste."""
    o = db.execute("SELECT * FROM orcamentos WHERE id=?", (oid,)).fetchone()
    v = db.execute("SELECT * FROM vendas WHERE orcamento_id=? AND status='ativa'", (oid,)).fetchone()
    if not o or not v:
        return
    itens, cpv, pend = custos_orcamento(db, oid, v["id"])
    bruto = o["total"] + o["desconto"]
    novo = (o["cliente_id"], o["total"], bruto, o["desconto"], cpv, pend)
    if novo == (v["cliente_id"], v["total"], v["valor_bruto"], v["desconto"], v["cpv"],
                v["cpv_pendentes"]):
        return
    db.execute("UPDATE vendas SET cliente_id=?, total=?, valor_bruto=?, desconto=?, cpv=?, "
               "cpv_pendentes=? WHERE id=?", novo + (v["id"],))
    gravar_custos(db, v["id"], itens)
    evento_venda(db, v["id"], "ajustada", v["total"], o["total"], "orçamento editado")


def estornar_venda(db, oid, motivo):
    v = db.execute("SELECT * FROM vendas WHERE orcamento_id=? AND status='ativa'", (oid,)).fetchone()
    if v:
        db.execute("UPDATE vendas SET status='cancelada' WHERE id=?", (v["id"],))
        evento_venda(db, v["id"], "estornada", v["total"], 0, motivo)
    return bool(v)


def mudar_status(db, oid, novo):
    """Troca a situação do orçamento (sem commit). Devolve (mensagem, aviso_de_estoque)."""
    o = db.execute("SELECT status, estoque_baixado FROM orcamentos WHERE id=?", (oid,)).fetchone()
    aprovar = novo in ("aprovado", "producao", "concluido")
    baixado = o["estoque_baixado"]
    msg = f"Situação alterada para: {ORC[novo][0]}."
    falta = ""
    if aprovar and not baixado:
        # a marca é gravada primeiro e só quem a trocou de 0 para 1 dá a baixa (sem duplicar)
        if db.execute("UPDATE orcamentos SET estoque_baixado=1 WHERE id=? AND estoque_baixado=0",
                      (oid,)).rowcount:
            falta = faltas(db, oid)
            aplicar_estoque(db, oid, True)
            msg += " Os produtos foram descontados do estoque."
        baixado = 1
    elif not aprovar and baixado:
        if db.execute("UPDATE orcamentos SET estoque_baixado=0 WHERE id=? AND estoque_baixado=1",
                      (oid,)).rowcount:
            aplicar_estoque(db, oid, False)
            msg += " Os produtos voltaram para o estoque."
        baixado = 0
    agora = agora_txt()
    db.execute("UPDATE orcamentos SET status=?, estoque_baixado=?, aprovado_em=CASE WHEN ? IN "
               "('aprovado','producao','concluido') THEN COALESCE(aprovado_em, ?) ELSE NULL END, "
               "concluido_em=CASE WHEN ?='concluido' THEN COALESCE(concluido_em, ?) ELSE NULL END "
               "WHERE id=?", (novo, baixado, novo, agora, novo, agora, oid))
    if novo == "concluido":
        if registrar_venda(db, oid):
            msg += " Venda registrada nos relatórios financeiros."
    elif o["status"] == "concluido" and estornar_venda(db, oid, f"orçamento voltou para {ORC[novo][0]}"):
        msg += " A venda foi estornada nos relatórios (o histórico foi mantido)."
    return msg, falta


@app.route("/orcamentos/<int:oid>/status", methods=["POST"])
def orcamento_status(oid):
    db = get_db()
    o = db.execute("SELECT status FROM orcamentos WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    novo = request.form.get("status")
    if novo not in ORC or novo == o["status"]:
        return redirect(url_for("orcamento_ver", oid=oid))
    try:
        msg, falta = mudar_status(db, oid, novo)
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Falha ao mudar a situação do orçamento")
        flash("Não foi possível alterar a situação. Nada foi alterado.", "erro")
        return redirect(url_for("orcamento_ver", oid=oid))
    flash(msg, "ok")
    if falta:
        flash("Aviso: estoque insuficiente, o saldo ficou negativo em: " + falta + ".", "aviso")
    return redirect(url_for("orcamento_ver", oid=oid))


@app.route("/orcamentos/<int:oid>/duplicar", methods=["POST"])
def orcamento_duplicar(oid):
    db = get_db()
    o = db.execute("SELECT * FROM orcamentos WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    cur = db.execute(
        "INSERT INTO orcamentos(cliente_id,titulo,data,med_l,med_a,med_p,perda,desconto,total,"
        "prazo,obs,desconto_tipo,desconto_valor,vendedor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o["cliente_id"], ((o["titulo"] or "") + " (cópia)").strip(),
         datetime.now().isoformat(" ", "seconds"), o["med_l"], o["med_a"], o["med_p"],
         o["perda"], o["desconto"], o["total"], o["prazo"], o["obs"], o["desconto_tipo"],
         o["desconto_valor"], o["vendedor"]))
    novo = cur.lastrowid
    mapa = {}
    for l in db.execute("SELECT * FROM orc_linhas WHERE orcamento_id=? ORDER BY ordem, id",
                        (oid,)).fetchall():
        c2 = db.execute(
            "INSERT INTO orc_linhas(orcamento_id,ordem,nome,descricao,largura,altura,qtd,perda,"
            "valor_unit,total,largura_calc,altura_calc,arred5) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novo, l["ordem"], l["nome"], l["descricao"], l["largura"], l["altura"], l["qtd"],
             l["perda"], l["valor_unit"], l["total"], l["largura_calc"], l["altura_calc"],
             l["arred5"]))
        mapa[l["id"]] = c2.lastrowid
    for i in db.execute("SELECT * FROM orcamento_itens WHERE orcamento_id=? ORDER BY id",
                        (oid,)).fetchall():
        db.execute(
            "INSERT INTO orcamento_itens(orcamento_id,produto_id,descricao,unidade,larg,alt,"
            "pecas,qtd,preco_unit,subtotal,linha_id,categoria,qtd_unit,sub_unit,larg_ov,alt_ov,"
            "manual,espessura,perda,minimo,calc_unit,desc_rateio) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novo, i["produto_id"], i["descricao"], i["unidade"], i["larg"], i["alt"],
             i["pecas"], i["qtd"], i["preco_unit"], i["subtotal"], mapa.get(i["linha_id"]),
             i["categoria"], i["qtd_unit"], i["sub_unit"], i["larg_ov"], i["alt_ov"],
             i["manual"], i["espessura"], i["perda"], i["minimo"], i["calc_unit"],
             i["desc_rateio"]))
    db.commit()
    flash("Orçamento duplicado (com os preços do original). Ajuste o que precisar e salve.", "ok")
    return redirect(url_for("orcamento_form", oid=novo))


@app.route("/orcamentos/<int:oid>/excluir", methods=["POST"])
def orcamento_excluir(oid):
    db = get_db()
    o = db.execute("SELECT estoque_baixado FROM orcamentos WHERE id=?", (oid,)).fetchone()
    estornar_venda(db, oid, "orçamento excluído")
    if o and o["estoque_baixado"]:
        aplicar_estoque(db, oid, False)  # devolve ao estoque antes de apagar
    db.execute("DELETE FROM orcamento_itens WHERE orcamento_id=?", (oid,))
    db.execute("DELETE FROM orc_linhas WHERE orcamento_id=?", (oid,))
    db.execute("DELETE FROM orcamentos WHERE id=?", (oid,))
    db.commit()
    flash("Orçamento excluído.", "ok")
    return redirect(url_for("orcamentos"))


# --------------------------------- Vendas --------------------------------
@app.route("/vendas")
def vendas():
    rows = get_db().execute(
        "SELECT v.*, c.nome cliente FROM vendas v LEFT JOIN clientes c ON c.id=v.cliente_id "
        "ORDER BY v.id DESC LIMIT 300").fetchall()
    return render_template("vendas.html", titulo="Vendas", rows=rows)


@app.route("/vendas/nova", methods=["GET", "POST"])
def venda_nova():
    db = get_db()
    cl = db.execute("SELECT id, nome FROM clientes ORDER BY nome").fetchall()
    prod_json = lista_produtos_json(db, somente_un=True)

    def tela():
        return render_template("venda_nova.html", titulo="Nova venda", cl=cl, prod=prod_json)

    if request.method == "POST":
        f = request.form
        ids, qtds, precos = f.getlist("produto_id"), f.getlist("qtd"), f.getlist("preco")
        itens = []
        try:
            for i, q, pc in zip(ids, qtds, precos):
                if not i:
                    continue
                itens.append((int(i), int(q), centavos(pc)))
        except ValueError:
            flash("Confira quantidades e preços dos itens.", "erro")
            return tela()
        if not itens or any(q <= 0 or p < 0 for _, q, p in itens):
            flash("Adicione ao menos um item com quantidade maior que zero.", "erro")
            return tela()

        info = {r["id"]: r for r in db.execute("SELECT * FROM produtos WHERE ativo=1")}
        soma = {}
        for pid, q, _ in itens:
            soma[pid] = soma.get(pid, 0) + q
        negativos = []
        for pid, q in soma.items():
            p = info.get(pid)
            if not p:
                flash("Produto não encontrado.", "erro")
                return tela()
            if controla_estoque(p) and p["estoque"] < q:   # só aviso: o saldo pode ficar negativo
                negativos.append(f"{p['nome']} (saldo {num_f(p['estoque'])} → {num_f(p['estoque'] - q)})")

        total = sum(q * p for _, q, p in itens)
        cid = inteiro(f.get("cliente_id"), 0) or None
        custos, cpv, pend = [], 0, 0
        for pid_, q_, _ in itens:                     # custo da venda, com os custos de hoje
            pr = info[pid_]
            if pr["servico"] and not pr["custo"]:
                continue
            if not pr["custo"]:
                pend += 1
                custos.append((pid_, pr["nome"], q_, None, None, 0))
            else:
                cpv += q_ * pr["custo"]
                custos.append((pid_, pr["nome"], q_, pr["custo"], q_ * pr["custo"], 1))
        cur = db.execute("INSERT INTO vendas(cliente_id,data,total,obs,valor_bruto,desconto,cpv,"
                         "cpv_pendentes) VALUES(?,?,?,?,?,0,?,?)",
                         (cid, datetime.now().isoformat(" ", "seconds"), total,
                          f.get("obs", "").strip(), total, cpv, pend))
        vid = cur.lastrowid
        gravar_custos(db, vid, custos)
        evento_venda(db, vid, "registrada", None, total)
        for pid, q, p in itens:
            db.execute("INSERT INTO itens_venda(venda_id,produto_id,qtd,preco_unit) "
                       "VALUES(?,?,?,?)", (vid, pid, q, p))
            if controla_estoque(info[pid]):
                mover_estoque(db, pid, -q, "saida_venda", venda_id=vid)
        db.commit()
        flash("Venda registrada e estoque atualizado.", "ok")
        if negativos:
            flash("Aviso: estoque insuficiente, o saldo ficou negativo em: " + "; ".join(negativos) + ".", "aviso")
        return redirect(url_for("venda_ver", vid=vid))
    return tela()


@app.route("/vendas/<int:vid>")
def venda_ver(vid):
    db = get_db()
    v = db.execute("SELECT v.*, c.nome cliente FROM vendas v LEFT JOIN clientes c "
                   "ON c.id=v.cliente_id WHERE v.id=?", (vid,)).fetchone()
    if not v:
        abort(404)
    itens = db.execute("SELECT i.*, p.nome FROM itens_venda i JOIN produtos p "
                       "ON p.id=i.produto_id WHERE i.venda_id=?", (vid,)).fetchall()
    linhas = []
    if v["orcamento_id"]:
        linhas = db.execute("SELECT * FROM orc_linhas WHERE orcamento_id=? ORDER BY ordem, id",
                            (v["orcamento_id"],)).fetchall()
    orc_existe = bool(v["orcamento_id"] and db.execute(
        "SELECT 1 FROM orcamentos WHERE id=?", (v["orcamento_id"],)).fetchone())
    eventos = db.execute("SELECT * FROM venda_eventos WHERE venda_id=? ORDER BY id", (vid,)).fetchall()
    return render_template("venda_ver.html", titulo=f"Venda #{vid}", v=v, itens=itens,
                           linhas=linhas, orc_existe=orc_existe, eventos=eventos,
                           hoje=datetime.now().strftime("%Y-%m-%d"))


@app.route("/vendas/<int:vid>/cancelar", methods=["POST"])
def venda_cancelar(vid):
    db = get_db()
    v = db.execute("SELECT * FROM vendas WHERE id=?", (vid,)).fetchone()
    if not v:
        abort(404)
    if v["status"] == "ativa" and v["orcamento_id"] and db.execute(
            "SELECT 1 FROM orcamentos WHERE id=?", (v["orcamento_id"],)).fetchone():
        # venda de orçamento: volta o orçamento para Aprovado (o estoque segue a regra do orçamento)
        try:
            msg, _ = mudar_status(db, v["orcamento_id"], "aprovado")
            db.commit()
        except Exception:
            db.rollback()
            flash("Não foi possível estornar a venda. Nada foi alterado.", "erro")
            return redirect(url_for("venda_ver", vid=vid))
        flash("Venda estornada. O orçamento voltou para Aprovado e o estoque continua baixado.", "ok")
    elif v["status"] == "ativa":
        for i in db.execute("SELECT i.produto_id, i.qtd, p.unidade, p.servico FROM itens_venda i "
                            "JOIN produtos p ON p.id=i.produto_id WHERE i.venda_id=?", (vid,)):
            if controla_estoque(i):
                mover_estoque(db, i["produto_id"], i["qtd"], "estorno_venda", venda_id=vid)
        db.execute("UPDATE vendas SET status='cancelada' WHERE id=?", (vid,))
        evento_venda(db, vid, "estornada", v["total"], 0, "venda cancelada")
        db.commit()
        flash("Venda cancelada e produtos devolvidos ao estoque.", "ok")
    return redirect(url_for("venda_ver", vid=vid))


@app.route("/vendas/<int:vid>/recebimento", methods=["POST"])
def venda_recebimento(vid):
    """Recebimento é outra coisa: a venda já é receita desde a conclusão; aqui só se marca o caixa."""
    db = get_db()
    v = db.execute("SELECT status FROM vendas WHERE id=?", (vid,)).fetchone()
    if not v:
        abort(404)
    if v["status"] != "ativa":
        flash("Venda cancelada não recebe pagamento.", "erro")
        return redirect(url_for("venda_ver", vid=vid))
    if request.form.get("desfazer"):
        db.execute("UPDATE vendas SET recebido_em=NULL WHERE id=?", (vid,))
        evento_venda(db, vid, "recebimento desfeito")
        flash("Recebimento desfeito. A venda continua contada como receita.", "ok")
    else:
        try:
            dia_ = request.form.get("recebido_em", "").strip()
            datetime.strptime(dia_, "%Y-%m-%d")
        except ValueError:
            flash("Informe a data do recebimento.", "erro")
            return redirect(url_for("venda_ver", vid=vid))
        db.execute("UPDATE vendas SET recebido_em=? WHERE id=?", (dia_, vid))
        evento_venda(db, vid, "recebimento registrado", None, None, dia_)
        flash("Recebimento registrado (a receita da venda não é somada de novo).", "ok")
    db.commit()
    return redirect(url_for("venda_ver", vid=vid))


# --------------------------------- Agenda --------------------------------
@app.route("/agenda")
def agenda():
    ver = request.args.get("ver", "abertos")
    db = get_db()
    base = ("SELECT a.*, c.nome cliente FROM agenda a LEFT JOIN clientes c "
            "ON c.id=a.cliente_id ")
    if ver == "todos":
        rows = db.execute(base + "ORDER BY a.data_hora DESC LIMIT 300").fetchall()
    else:
        rows = db.execute(base + "WHERE a.status='agendado' ORDER BY a.data_hora").fetchall()
    agora = datetime.now().strftime("%Y-%m-%dT%H:%M")
    return render_template("agenda.html", titulo="Agenda", rows=rows, ver=ver, agora=agora)


@app.route("/agenda/novo", methods=["GET", "POST"])
@app.route("/agenda/<int:aid>/editar", methods=["GET", "POST"])
def agenda_form(aid=None):
    db = get_db()
    a = db.execute("SELECT * FROM agenda WHERE id=?", (aid,)).fetchone() if aid else None
    if aid and not a:
        abort(404)
    cl = db.execute("SELECT id, nome FROM clientes ORDER BY nome").fetchall()
    if request.method == "POST":
        f = request.form
        titulo = f.get("titulo", "").strip()
        data_hora = f.get("data_hora", "").strip()
        if not titulo or not data_hora:
            flash("Informe o título e a data/hora.", "erro")
            return render_template("agenda_form.html", titulo="Compromisso", a=f, cl=cl, aid=aid)
        cid = inteiro(f.get("cliente_id"), 0) or None
        dados = (cid, titulo, data_hora, f.get("obs", "").strip())
        if aid:
            db.execute("UPDATE agenda SET cliente_id=?,titulo=?,data_hora=?,obs=? WHERE id=?",
                       dados + (aid,))
        else:
            db.execute("INSERT INTO agenda(cliente_id,titulo,data_hora,obs) VALUES(?,?,?,?)", dados)
        db.commit()
        flash("Compromisso salvo.", "ok")
        return redirect(url_for("agenda"))
    return render_template("agenda_form.html", titulo="Compromisso", a=a, cl=cl, aid=aid)


@app.route("/agenda/<int:aid>/status", methods=["POST"])
def agenda_status(aid):
    novo = request.form.get("status")
    if novo in ("agendado", "concluido", "cancelado"):
        db = get_db()
        db.execute("UPDATE agenda SET status=? WHERE id=?", (novo, aid))
        db.commit()
    return redirect(request.referrer or url_for("agenda"))


@app.route("/agenda/<int:aid>/excluir", methods=["POST"])
def agenda_excluir(aid):
    db = get_db()
    db.execute("DELETE FROM agenda WHERE id=?", (aid,))
    db.commit()
    flash("Compromisso excluído.", "ok")
    return redirect(url_for("agenda"))


# --------------------------------- Backup --------------------------------
@app.route("/backup")
def backup():
    copia = os.path.join(PASTA, "backup_temp.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(copia)
    src.backup(dst)
    dst.close()
    src.close()
    nome = f"backup_gestao_{datetime.now():%Y%m%d_%H%M}.db"
    return send_file(copia, as_attachment=True, download_name=nome)


# -------------------------------- Empresa --------------------------------
def so_digitos(t):
    return re.sub(r"\D", "", t or "")


def cnpj_valido(d):
    if len(d) != 14 or d == d[0] * 14:
        return False
    for n in (12, 13):
        pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        dv = sum(int(c) * p for c, p in zip(d[:n], pesos)) % 11
        if int(d[n]) != (0 if dv < 2 else 11 - dv):
            return False
    return True


def mascara_cnpj(d):
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def mascara_tel(d):
    return f"({d[:2]}) {d[2:7]}-{d[7:]}" if len(d) == 11 else f"({d[:2]}) {d[2:6]}-{d[6:]}"


def validar_empresa(f):
    """Valida só os campos enviados. Devolve (valores, erros)."""
    v, erros = {}, []
    for k in CAMPOS_EMPRESA:
        if k in f:
            v[k] = f.get(k, "").strip()[:500 if k in ("obs_inst", "rodape") else 150]
    if v.get("cnpj"):
        d = so_digitos(v["cnpj"])
        if cnpj_valido(d):
            v["cnpj"] = mascara_cnpj(d)
        else:
            erros.append("CNPJ inválido.")
    for k, nome in (("telefone", "Telefone"), ("whatsapp", "WhatsApp")):
        if v.get(k):
            d = so_digitos(v[k])
            if len(d) in (10, 11):
                v[k] = mascara_tel(d)
            else:
                erros.append(f"{nome} inválido (use DDD + número).")
    if v.get("cep"):
        d = so_digitos(v["cep"])
        if len(d) == 8:
            v["cep"] = f"{d[:5]}-{d[5:]}"
        else:
            erros.append("CEP inválido (8 dígitos).")
    if v.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
        erros.append("E-mail inválido.")
    if v.get("estado"):
        v["estado"] = v["estado"].upper()
        if v["estado"] not in UFS:
            erros.append("Estado inválido (use a sigla, ex.: SP).")
    if v.get("site") and not re.fullmatch(r"(https?://)?[^\s/]+\.[^\s]+", v["site"]):
        erros.append("Site inválido.")
    return v, erros


def ler_logo(arq):
    """Valida o arquivo enviado: só PNG ou JPG (confere o conteúdo, não só a extensão), até 1 MB."""
    dados = arq.read(LOGO_MAX + 1)
    if len(dados) > LOGO_MAX:
        return None, None, "O logotipo deve ter no máximo 1 MB."
    if dados[:8] == b"\x89PNG\r\n\x1a\n":
        return dados, "image/png", None
    if dados[:3] == b"\xff\xd8\xff":
        return dados, "image/jpeg", None
    return None, None, "Formato de logotipo não aceito: envie uma imagem PNG ou JPG."


@app.route("/empresa", methods=["GET", "POST"])
def empresa_pagina():
    db = get_db()
    if request.method == "POST":
        v, erros = validar_empresa(request.form)
        arq = request.files.get("logo")
        logo = None
        if arq and arq.filename:
            dados, tipo, erro = ler_logo(arq)
            if erro:
                erros.append(erro)
            else:
                logo = (dados, tipo)
        if erros:
            for e in erros:
                flash(e, "erro")
            dados_form = empresa_dados(db)
            dados_form.update({k: request.form.get(k, dados_form.get(k, "")) for k in CAMPOS_EMPRESA})
            return render_template("empresa.html", titulo="Empresa", e=dados_form, ufs=UFS), 400
        sets, par = [], []
        for k, val in v.items():
            sets.append(f"{k}=?"); par.append(val)
        if logo:
            lp, lt = logo_leve(logo[0], logo[1])
            sets += ["logo=?", "logo_tipo=?", "logo_print=?", "logo_print_tipo=?"]
            par += [logo[0], logo[1], lp, lt]
        elif request.form.get("remover_logo"):
            sets += ["logo=NULL", "logo_tipo=NULL", "logo_print=NULL", "logo_print_tipo=NULL"]
        sets.append("atualizado_em=?"); par.append(agora_txt())
        db.execute(f"UPDATE empresa SET {','.join(sets)} WHERE id=1", par)
        db.commit()
        flash("Dados da empresa salvos.", "ok")
        return redirect(url_for("empresa_pagina"))
    return render_template("empresa.html", titulo="Empresa", e=empresa_dados(db), ufs=UFS)


@app.route("/empresa/logo")
def empresa_logo():
    r = get_db().execute("SELECT logo, logo_tipo, logo_print, logo_print_tipo FROM empresa "
                         "WHERE id=1").fetchone()
    if not r or not r["logo"]:
        abort(404)
    if request.args.get("print") and r["logo_print"]:
        dados, tipo = r["logo_print"], r["logo_print_tipo"]
    else:
        dados, tipo = r["logo"], r["logo_tipo"]
    resp = app.response_class(dados, mimetype=tipo or "image/png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # a URL leva ?v=<data da última alteração>: pode ser guardada no navegador sem risco
    resp.headers["Cache-Control"] = "public, max-age=86400" if request.args.get("v") else "no-cache"
    return resp


# -------------------------------- Templates ------------------------------
BASE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ titulo }} · {{ empresa }}</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--tx:#1f2933;--mut:#6b7785;--pri:#2563eb;--bd:#e1e6ec;--er:#dc2626}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,"Segoe UI",sans-serif;background:var(--bg);color:var(--tx)}
.side{position:fixed;left:0;top:0;bottom:0;width:224px;background:#0f172a;padding:14px 12px;display:flex;flex-direction:column;gap:2px;overflow:auto;z-index:20}
.side .brand{color:#fff;font-weight:700;font-size:18px;padding:8px 10px 18px}
.side a{color:#cbd5e1;text-decoration:none;padding:10px 12px;border-radius:8px;display:block}
.side a:hover{background:#1e293b;color:#fff}
.side a.on{background:var(--pri);color:#fff}
.side a.fim{margin-top:auto;font-size:13px}
.wrap{margin-left:224px}
main{max-width:1180px;margin:24px auto;padding:0 20px}
@media(max-width:900px){
 .side{position:static;width:auto;flex-direction:row;flex-wrap:wrap;align-items:center;padding:6px 8px;gap:0}
 .side .brand{padding:8px 10px}.side a.fim{margin:0 0 0 auto}.wrap{margin-left:0}main{padding:0 12px;margin:16px auto}}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.grid .card{margin:0}
.kpi small{color:var(--mut);display:block}.kpi b{font-size:22px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-size:13px;font-weight:600}
td.n,th.n{text-align:right}
input,select,textarea{width:100%;padding:8px;border:1px solid var(--bd);border-radius:8px;font:inherit;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid #bfdbfe;border-color:var(--pri)}
input[type=checkbox]{width:auto}
label{display:block;margin:10px 0 4px;color:var(--mut);font-size:13px}
.btn{display:inline-block;background:var(--pri);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer;text-decoration:none}
.btn:disabled{opacity:.6;cursor:wait}
.btn.sec{background:#e5e9ef;color:var(--tx)}.btn.red{background:var(--er)}.btn.sm{padding:3px 10px;font-size:13px}
.bar{display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap}
.bar form{flex:1;display:flex;gap:8px;min-width:200px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:12px}
.flash.aviso{background:#fef3c7;color:#78350f}.flash.ok{background:#dcfce7;color:#14532d}.flash.erro{background:#fee2e2;color:#7f1d1d}
.tag{padding:2px 8px;border-radius:99px;font-size:12px;background:#e5e9ef;white-space:nowrap;display:inline-block}
.tag.ok{background:#dcfce7;color:#14532d}.tag.er{background:#fee2e2;color:#7f1d1d}.tag.wa{background:#fef3c7;color:#78350f}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.row4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:600px){.row2,.row4{grid-template-columns:1fr 1fr}}
form.inline{display:inline}.mut{color:var(--mut)}
.total{font-size:20px;text-align:right;margin:12px 0}
.hint{font-size:12px;color:var(--mut);margin-top:4px}
.cab{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:12px}
@media print{.side,.noprint,.flash{display:none!important}body{background:#fff}.wrap{margin:0}
main{margin:0;max-width:none;padding:0}.card{border:0;padding:0}}
</style>
{% block head %}{% endblock %}
</head><body>
<aside class="side"><div class="brand">{{ empresa }}</div>
{% for path, nome in [('/','🏠 Início'),('/clientes','👥 Clientes'),('/orcamentos','📋 Orçamentos'),('/produtos','📦 Produtos'),('/vendas','🛒 Vendas'),('/avaliacoes','⭐ Avaliações'),('/financeiro','💰 Financeiro'),('/bancos','🏦 Bancos'),('/agenda','📅 Agenda'),('/empresa','🏢 Empresa')] %}
<a href="{{ path }}" class="{{ 'on' if (path == '/' and request.path == '/') or (path != '/' and request.path.startswith(path)) else '' }}">{{ nome }}</a>
{% endfor %}
<a class="fim" href="{{ url_for('backup') }}">⬇ Backup</a>
</aside>
<div class="wrap"><main>
{% for cat, msg in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}
{% block conteudo %}{% endblock %}
</main></div></body></html>"""

DASHBOARD = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Início</h1>
<div class="grid">
  <div class="card kpi"><small>Orçamentos em aberto</small><b>{{ ab.n }}</b><small>{{ ab.t|brl }}</small></div>
  <div class="card kpi"><small>Orçamentos aprovados no mês</small><b>{{ ap.t|brl }}</b><small>{{ ap.n }} orçamento(s)</small></div>
  <div class="card kpi"><small>Vendas no mês</small><b>{{ v.t|brl }}</b><small>{{ v.n }} venda(s)</small></div>
  <div class="card kpi"><small>Capital investido em estoque</small><b>{{ investido|brl }}</b><small><a href="{{ url_for('produtos') }}">ver estoque</a></small></div>
  <div class="card kpi"><small>Clientes</small><b>{{ n_clientes }}</b></div>
</div>
{% if atrasados %}<div class="flash erro">Você tem {{ atrasados }} compromisso(s) da agenda com data vencida e ainda em aberto. <a href="{{ url_for('agenda') }}">Ver agenda</a></div>{% endif %}
<div class="card"><h2>Orçamentos em aberto</h2>
{% if abertos %}<table><tr><th>#</th><th>Cliente</th><th>Peça</th><th>Data</th><th class="n">Total</th></tr>
{% for o in abertos %}<tr><td><a href="{{ url_for('orcamento_ver', oid=o.id) }}">{{ o.id }}</a></td><td>{{ o.cliente }}</td><td>{{ o.titulo or '' }}</td><td>{{ o.data|dt }}</td><td class="n">{{ o.total|brl }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum orçamento em aberto.</p>{% endif %}
<a class="btn sm" href="{{ url_for('orcamento_form') }}">+ Novo orçamento</a></div>
<div class="row2">
  <div class="card"><h2>Próximos compromissos</h2>
  {% if prox %}<table>{% for a in prox %}<tr><td>{{ a.data_hora|dt }}</td><td>{{ a.titulo }}{% if a.cliente %} <span class="mut">· {{ a.cliente }}</span>{% endif %}</td></tr>{% endfor %}</table>
  {% else %}<p class="mut">Nada agendado.</p>{% endif %}
  <a class="btn sm" href="{{ url_for('agenda_form') }}">+ Novo compromisso</a></div>
  <div class="card"><h2>Produtos com estoque baixo</h2>
  {% if baixo %}<table>{% for p in baixo %}<tr><td>{{ p.nome }}</td><td class="n"><span class="tag {{ 'er' if p.estoque <= 0 else 'wa' }}">{{ p.estoque|num(4) }} {{ p.unidade|ul }}</span></td></tr>{% endfor %}</table>
  {% else %}<p class="mut">Tudo certo com o estoque.</p>{% endif %}</div>
</div>
<div class="card"><h2>Últimas vendas</h2>
{% if ultimas %}<table><tr><th>#</th><th>Data</th><th>Cliente</th><th class="n">Total</th><th></th></tr>
{% for x in ultimas %}<tr><td><a href="{{ url_for('venda_ver', vid=x.id) }}">{{ x.id }}</a></td><td>{{ x.data|dt }}</td><td>{{ x.cliente or 'Consumidor' }}</td><td class="n">{{ x.total|brl }}</td><td>{% if x.status == 'cancelada' %}<span class="tag er">cancelada</span>{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhuma venda ainda.</p>{% endif %}
<a class="btn sm" href="{{ url_for('venda_nova') }}">+ Nova venda</a></div>
{% endblock %}"""

FINANCEIRO = r"""{% extends "base.html" %}{% block head %}<style>
.fin-top{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.fin-top select,.fin-top input{width:auto}
.fin-top .f{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}.fin-top label{margin:0 0 3px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.kpis .card{margin:0;padding:14px 16px}
.kpis small{color:var(--mut);display:block;font-size:12.5px}.kpis b{font-size:22px;display:block;margin:2px 0;font-variant-numeric:tabular-nums}
.pos{color:#15803d}.neg{color:#b91c1c}
.alerta{background:#fffbeb;border:1px solid #fde68a;color:#78350f;border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:14px}
.chart-h{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.seg button{border:1px solid var(--bd);background:#fff;padding:3px 10px;font:inherit;font-size:13px;cursor:pointer}
.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0;border-left:0}
.seg button.on{background:#eff6ff;color:var(--pri);border-color:#bfdbfe}
#graf{position:relative}#graf svg{width:100%;height:clamp(220px,36vh,320px);display:block}
#tip{position:absolute;pointer-events:none;background:#0f172a;color:#fff;border-radius:8px;padding:6px 10px;font-size:12.5px;white-space:nowrap;display:none;z-index:5}
.fin-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:12px}
.fin-bar select{width:auto}.fin-bar .sp{flex:1}
.dre td,.dre th{padding:7px 8px}.dre td.n{font-variant-numeric:tabular-nums;white-space:nowrap}
.dre tr.sub td{font-weight:700;background:#f8fafc}.dre tr.ded td:first-child{padding-left:22px;color:var(--mut)}
.dre tr.fim td{font-weight:700;font-size:16px;border-top:2px solid var(--bd)}
dialog{border:0;border-radius:14px;padding:20px;width:min(520px,94vw);box-shadow:0 20px 50px #0004}
dialog::backdrop{background:#0f172a66}dialog h2{margin-top:0}
.nota{font-size:12.5px;color:var(--mut)}
.btn.ghost{background:transparent;color:var(--pri);border:1px solid var(--bd)}
td.ac{white-space:nowrap;text-align:right}
</style>{% endblock %}{% block conteudo %}
<div class="fin-top"><div><h1>Financeiro</h1><span class="mut" id="perLabel"></span></div>
<div class="f"><div><label>Mês</label><select id="fMes"><option value="0">Ano todo</option>{% for m in meses %}<option value="{{ loop.index }}"{{ ' selected' if loop.index == mes_atual else '' }}>{{ m }}</option>{% endfor %}</select></div>
<div><label>Ano</label><input type="number" id="fAno" value="{{ ano_atual }}" min="2000" max="2100" style="width:96px"></div></div></div>

<div class="kpis">
 <div class="card"><small>Total de vendas concluídas</small><b id="kVendas">—</b><small>receita líquida das vendas</small></div>
 <div class="card"><small>Quantidade de vendas</small><b id="kQtd">—</b><small>pedidos concluídos</small></div>
 <div class="card"><small>Ticket médio</small><b id="kTicket">—</b><small>vendas ÷ quantidade</small></div>
 <div class="card"><small>Total de receitas</small><b id="kRec">—</b><small>vendas + receitas avulsas</small></div>
 <div class="card"><small>Total de despesas</small><b id="kDesp">—</b><small id="kDespSub"></small></div>
 <div class="card"><small>Resultado líquido gerencial</small><b id="kRes">—</b><small id="kResSub">DRE gerencial</small></div>
</div>
<div id="avisoCpv" class="alerta" style="display:none"></div>

<div class="card"><div class="chart-h"><h2>Evolução das Vendas</h2>
<div class="seg"><button id="vGraf" class="on">Gráfico</button><button id="vTab">Tabela</button></div></div>
<div id="graf"><div id="tip"></div><svg id="svg" viewBox="0 0 760 250" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Vendas por mês"></svg></div>
<div id="tabela" style="display:none"></div></div>

<div class="card"><div class="chart-h"><h2>Controle Financeiro</h2>
<div class="bar noprint" style="margin:0"><button class="btn sm" id="bRec">+ Receita</button><button class="btn red sm" id="bDesp">+ Despesa</button><button class="btn ghost sm" id="bCat">Categorias</button></div></div>
<p class="nota">Vendas concluídas entram sozinhas como receita. Aqui você lança só o que não vem de vendas: outras receitas e as despesas.</p>
<div class="fin-bar"><div><label>Categoria</label><select id="lCat"><option value="">Todas</option></select></div>
<div><label>Tipo</label><select id="lTipo"><option value="">Todos</option><option value="venda">Vendas (automático)</option><option value="receita">Receitas avulsas</option><option value="despesa">Despesas</option></select></div></div>
<div id="lista"></div></div>

<div class="card"><h2>DRE Gerencial — Demonstrativo de Resultados</h2>
<p class="nota" id="dreSub"></p><div id="dre"></div><div id="caixa"></div></div>

<dialog id="dLanc"><form id="fLanc" method="dialog"><h2 id="dTitulo">Lançamento</h2>
<input type="hidden" id="lId"><input type="hidden" id="lTipoF">
<label>Descrição</label><input id="lDesc" maxlength="120">
<div class="row2"><div><label>Categoria</label><select id="lCatF"></select></div><div><label>Valor (R$)</label><input id="lVal" inputmode="decimal" placeholder="0,00"></div></div>
<div class="row2"><div><label id="lDataL">Data de competência</label><input type="date" id="lData"></div><div><label>Vencimento (opcional)</label><input type="date" id="lVenc"></div></div>
<div class="row2"><div><label>Situação</label><select id="lSit"></select></div><div id="lPagBox"><label id="lPagL">Data do pagamento</label><input type="date" id="lPag"></div></div>
<label>Observações</label><textarea id="lObs" rows="2"></textarea>
<div id="lErro" class="flash erro" style="display:none;margin-top:10px"></div>
<div class="bar" style="margin:14px 0 0;justify-content:flex-end"><button type="button" class="btn sec" id="lCancel">Cancelar</button><button type="button" class="btn" id="lSalvar">Salvar</button></div></form></dialog>

<dialog id="dCat"><h2>Categorias</h2><div id="catLista" style="max-height:40vh;overflow:auto"></div>
<div class="row2" style="margin-top:10px"><div><label>Nova categoria</label><input id="cNome" maxlength="60"></div><div><label>Tipo</label><select id="cTipo"><option value="despesa">Despesa</option><option value="receita">Receita</option></select></div></div>
<div id="cErro" class="flash erro" style="display:none;margin-top:10px"></div>
<div class="bar" style="margin:12px 0 0;justify-content:flex-end"><button class="btn sec" id="cFechar">Fechar</button><button class="btn" id="cAdd">Adicionar</button></div></dialog>

<script>
const MESES = {{ meses|tojson }};
const $ = id => document.getElementById(id);
const brl = c => (c / 100).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
const esc = t => String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const dbr = d => d ? d.slice(8, 10) + '/' + d.slice(5, 7) + '/' + d.slice(0, 4) : '';
const cls = c => c < 0 ? 'neg' : (c > 0 ? 'pos' : '');
let P = null, CATS = [], modo = 'g';

function periodo() { return 'ano=' + encodeURIComponent($('fAno').value) + '&mes=' + $('fMes').value; }
function rotulo() { const m = +$('fMes').value; return (m ? MESES[m - 1] + ' de ' : 'Ano de ') + $('fAno').value; }

async function carregar() {
  $('perLabel').textContent = rotulo();
  const r = await fetch('/api/painel?' + periodo()); P = await r.json();
  const d = P.dre, m = +$('fMes').value;
  $('kVendas').textContent = brl(d.vendas_liq);
  $('kQtd').textContent = d.vendas_qtd;
  $('kTicket').textContent = brl(d.ticket_medio);
  $('kRec').textContent = brl(d.receitas_total);
  $('kDesp').textContent = brl(d.despesas_card);
  $('kDespSub').textContent = d.despesas_pendentes ? brl(d.despesas_pendentes) + ' ainda a pagar' : 'despesas, impostos e outras (sem custo de estoque)';
  const k = $('kRes'); k.textContent = brl(d.resultado_liquido); k.className = cls(d.resultado_liquido);
  $('kResSub').textContent = d.cpv_incompleto ? 'parcial: custo incompleto' : 'DRE gerencial';
  const av = $('avisoCpv');
  if (d.cpv_incompleto) {
    const p = [];
    if (d.cpv_pendentes) p.push(d.cpv_pendentes + ' item(ns) vendido(s) sem preço de custo cadastrado');
    if (d.vendas_sem_custo) p.push(d.vendas_sem_custo + ' venda(s) antiga(s) sem custo registrado');
    av.style.display = 'block';
    av.textContent = 'Custo dos produtos vendidos incompleto: ' + p.join(' e ') + '. O lucro bruto e o resultado abaixo estão PARCIAIS (esses custos não foram tratados como zero de propósito, mas ficaram de fora). Cadastre o custo dos produtos para corrigir.';
  } else av.style.display = 'none';
  desenhar(); dre();
}

function desenhar() {
  const S = P.serie, W = 760, H = 250, L = 56, R = 10, T = 14, B = 30, iw = W - L - R, ih = H - T - B;
  const max = Math.max(...S.map(x => x.total), 0);
  let top = 100, passo = 100;
  if (max > 0) { const raw = max / 4, pot = Math.pow(10, Math.floor(Math.log10(raw))); passo = [1, 2, 2.5, 5, 10].map(f => f * pot).find(v => v >= raw); top = passo * 4; }
  const y = v => T + ih - (v / top) * ih, slot = iw / 12, bw = Math.min(26, slot * .5);
  let h = '';
  for (let i = 0; i <= 4; i++) { const v = passo * i, yy = y(v);
    h += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="#e5e9ef" stroke-width="1"/><text x="${L - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="#6b7785">${v >= 100000 ? 'R$ ' + (v / 100000).toLocaleString('pt-BR') + ' mil' : brl(v).replace(',00', '')}</text>`; }
  const sel = +$('fMes').value;
  let imax = max > 0 ? S.findIndex(x => x.total === max) : -1;
  S.forEach((x, i) => { const cx = L + slot * i + slot / 2, hh = Math.max(x.total ? (x.total / top) * ih : 0, 0), x0 = cx - bw / 2, y0 = T + ih - hh, r = Math.min(4, hh / 2);
    if (hh > 0) h += `<path d="M${x0},${T + ih} V${y0 + r} Q${x0},${y0} ${x0 + r},${y0} H${x0 + bw - r} Q${x0 + bw},${y0} ${x0 + bw},${y0 + r} V${T + ih} Z" fill="${!sel || sel === x.mes ? '#2563eb' : '#93b4f5'}"/>`;
    else h += `<line x1="${x0}" x2="${x0 + bw}" y1="${T + ih - .5}" y2="${T + ih - .5}" stroke="#cbd5e1" stroke-width="2"/>`;
    h += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="11.5" fill="#6b7785">${x.nome.slice(0, 3)}</text>`;
    if (i === imax) h += `<text x="${cx}" y="${y0 - 6}" text-anchor="middle" font-size="11.5" font-weight="600" fill="#1f2933">${brl(x.total)}</text>`;
    h += `<rect data-i="${i}" x="${L + slot * i}" y="${T}" width="${slot}" height="${ih + B}" fill="transparent"/>`; });
  $('svg').innerHTML = h;
  let t = '<table><tr><th>Mês</th><th class="n">Vendas</th><th class="n">Quantidade</th></tr>';
  S.forEach(x => t += `<tr><td>${x.nome}</td><td class="n">${brl(x.total)}</td><td class="n">${x.qtd}</td></tr>`);
  $('tabela').innerHTML = t + `<tr class="sub"><td><b>Total</b></td><td class="n"><b>${brl(S.reduce((a, x) => a + x.total, 0))}</b></td><td class="n"><b>${S.reduce((a, x) => a + x.qtd, 0)}</b></td></tr></table>`;
}
$('svg').addEventListener('mousemove', e => { const i = e.target.getAttribute && e.target.getAttribute('data-i'), tip = $('tip');
  if (i == null) { tip.style.display = 'none'; return; }
  const x = P.serie[+i], b = $('graf').getBoundingClientRect();
  tip.innerHTML = `<b>${x.nome}</b><br>${brl(x.total)} · ${x.qtd} venda(s)`; tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX - b.left + 12, b.width - 150) + 'px'; tip.style.top = (e.clientY - b.top - 48) + 'px'; });
$('svg').addEventListener('mouseleave', () => $('tip').style.display = 'none');
function modoVer(g) { $('graf').style.display = g ? '' : 'none'; $('tabela').style.display = g ? 'none' : ''; $('vGraf').className = g ? 'on' : ''; $('vTab').className = g ? '' : 'on'; }
$('vGraf').onclick = () => modoVer(true); $('vTab').onclick = () => modoVer(false);

function dre() {
  const d = P.dre, par = d.cpv_incompleto ? ' <span class="tag wa">parcial</span>' : '';
  const L = (n, v, c = '', neg = false) => `<tr class="${c}"><td>${n}</td><td class="n ${c === 'sub' || c === 'fim' ? cls(v) : ''}">${neg && v ? '− ' + brl(v) : brl(v)}</td></tr>`;
  $('dreSub').textContent = 'Regime de competência — ' + rotulo() + '. Venda concluída vira receita no dia da conclusão; recebimento do dinheiro aparece separado, abaixo.';
  $('dre').innerHTML = '<table class="dre">' +
    L('Receita bruta de vendas', d.receita_bruta) +
    L('(−) Descontos e deduções', d.descontos + d.deducoes, '', true) +
    (d.deducoes ? `<tr class="ded"><td>dos quais: impostos e deduções lançados</td><td class="n">− ${brl(d.deducoes)}</td></tr>` : '') +
    L('(=) Receita líquida', d.receita_liquida, 'sub') +
    `<tr><td>(−) Custo dos produtos vendidos (CPV)${par}</td><td class="n">${d.cpv_incompleto && !d.cpv ? 'indisponível' : '− ' + brl(d.cpv)}</td></tr>` +
    `<tr class="sub"><td>(=) Lucro bruto${par}</td><td class="n ${cls(d.lucro_bruto)}">${brl(d.lucro_bruto)}</td></tr>` +
    L('(−) Despesas operacionais', d.despesas_operacionais, '', true) +
    `<tr class="sub"><td>(=) Resultado operacional${par}</td><td class="n ${cls(d.resultado_operacional)}">${brl(d.resultado_operacional)}</td></tr>` +
    L('(+/−) Outras receitas e despesas', d.outras_liquido) +
    `<tr class="fim"><td>(=) Resultado líquido gerencial${par}</td><td class="n ${cls(d.resultado_liquido)}">${brl(d.resultado_liquido)}</td></tr></table>`;
  const c = d.caixa;
  $('caixa').innerHTML = '<h2 style="margin-top:18px">Caixa realizado (dinheiro que de fato entrou/saiu)</h2><table class="dre">' +
    L('Vendas recebidas', c.vendas_recebidas) + L('Outras receitas recebidas', c.receitas_recebidas) +
    L('Despesas pagas', c.despesas_pagas, '', true) + L('(=) Saldo de caixa do período', c.saldo, 'sub') +
    L('Vendas do período ainda a receber (já contam como receita)', c.vendas_a_receber) + '</table>' +
    '<p class="nota">Compras de materiais para estoque não são despesa do período: o custo entra no CPV quando o produto é vendido.</p>';
}

const SIT = {pendente: 'Pendente', pago: 'Pago', recebido: 'Recebido', 'a receber': 'A receber'};
async function lista() {
  const q = periodo() + '&tipo=' + $('lTipo').value + '&categoria=' + $('lCat').value;
  const r = await (await fetch('/api/financeiro/lancamentos?' + q)).json();
  if (!r.linhas.length) { $('lista').innerHTML = '<p class="mut">Nenhum lançamento neste período/filtro.</p>'; return; }
  let h = '<table><tr><th>Data</th><th>Descrição</th><th>Categoria</th><th>Tipo</th><th class="n">Valor</th><th>Status</th><th></th></tr>';
  r.linhas.forEach(x => {
    const venda = x.origem === 'venda', ok = x.status === 'pago' || x.status === 'recebido';
    const tp = venda ? 'Venda' : (x.tipo === 'receita' ? 'Receita' : 'Despesa');
    h += `<tr><td>${dbr(x.data)}</td><td>${venda ? `<a href="/vendas/${x.id}">${esc(x.descricao)}</a>` : esc(x.descricao)}</td><td>${esc(x.categoria)}</td><td>${tp}</td>
      <td class="n ${x.tipo === 'despesa' ? 'neg' : 'pos'}">${x.tipo === 'despesa' ? '− ' : ''}${brl(x.valor)}</td>
      <td><span class="tag ${ok ? 'ok' : 'wa'}">${SIT[x.status] || x.status}</span></td>
      <td class="ac">${venda ? '<span class="nota">automática</span>' : `<button class="btn sec sm" data-e="${x.id}">Editar</button> <button class="btn red sm" data-x="${x.id}">Excluir</button>`}</td></tr>`; });
  $('lista').innerHTML = h + '</table>';
  window.LINHAS = r.linhas;
}
$('lista').addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.e) { abrir(window.LINHAS.find(x => x.origem === 'lancamento' && x.id == b.dataset.e)); }
  if (b.dataset.x && confirm('Excluir este lançamento?')) { await fetch('/api/financeiro/lancamentos/' + b.dataset.x, {method: 'DELETE'}); atualizar(); }
});

async function cats() {
  CATS = (await (await fetch('/api/financeiro/categorias')).json()).categorias;
  const v = $('lCat').value; let h = '<option value="">Todas</option><option value="venda">Vendas (automático)</option>';
  CATS.forEach(c => h += `<option value="${c.id}">${esc(c.nome)} (${c.tipo})</option>`);
  $('lCat').innerHTML = h; $('lCat').value = v;
}
function sitOpts(tipo, atual) {
  const o = tipo === 'despesa' ? [['pendente', 'Pendente'], ['pago', 'Pago']] : [['pendente', 'A receber'], ['recebido', 'Recebido']];
  $('lSit').innerHTML = o.map(a => `<option value="${a[0]}">${a[1]}</option>`).join(''); $('lSit').value = atual || 'pendente';
  $('lPagL').textContent = tipo === 'despesa' ? 'Data do pagamento' : 'Data do recebimento';
  $('lPagBox').style.display = $('lSit').value === 'pendente' ? 'none' : '';
}
$('lSit').onchange = () => { $('lPagBox').style.display = $('lSit').value === 'pendente' ? 'none' : ''; };
function abrir(x, tipo) {
  const t = x ? x.tipo : tipo; $('lTipoF').value = t; $('lId').value = x ? x.id : '';
  $('dTitulo').textContent = (x ? 'Editar ' : 'Nova ') + (t === 'despesa' ? 'despesa' : 'receita');
  $('lCatF').innerHTML = CATS.filter(c => c.tipo === t).map(c => `<option value="${c.id}">${esc(c.nome)}</option>`).join('');
  $('lDesc').value = x ? x.descricao : ''; $('lVal').value = x ? (x.valor / 100).toFixed(2).replace('.', ',') : '';
  $('lData').value = x ? x.data : new Date().toISOString().slice(0, 10); $('lVenc').value = x && x.vencimento || '';
  if (x && x.categoria_id) $('lCatF').value = x.categoria_id;
  sitOpts(t, x && x.status); $('lPag').value = x && x.pagamento || ''; $('lObs').value = x ? x.obs : '';
  $('lDataL').textContent = t === 'despesa' ? 'Data da despesa' : 'Data de competência';
  $('lErro').style.display = 'none'; $('dLanc').showModal();
}
$('bRec').onclick = () => abrir(null, 'receita'); $('bDesp').onclick = () => abrir(null, 'despesa');
$('lCancel').onclick = () => $('dLanc').close();
$('lSalvar').onclick = async () => {
  const id = $('lId').value, body = {tipo: $('lTipoF').value, descricao: $('lDesc').value, categoria_id: $('lCatF').value, valor: $('lVal').value,
    data: $('lData').value, vencimento: $('lVenc').value, pagamento: $('lPag').value, status: $('lSit').value, obs: $('lObs').value};
  const r = await fetch('/api/financeiro/lancamentos' + (id ? '/' + id : ''), {method: id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!j.ok) { $('lErro').textContent = j.erros.join(' '); $('lErro').style.display = 'block'; return; }
  $('dLanc').close();
  // se o lançamento cair fora do período exibido, leva o filtro para o mês dele
  const [a, m] = body.data.split('-'); if ($('fMes').value !== '0' && (a !== $('fAno').value || +m !== +$('fMes').value)) { $('fAno').value = a; $('fMes').value = +m; }
  atualizar();
};
async function catLista() {
  await cats();
  $('catLista').innerHTML = '<table>' + CATS.map(c => `<tr><td>${esc(c.nome)}</td><td class="mut">${c.tipo}${c.grupo === 'estoque' ? ' · não é despesa' : ''}</td><td class="ac"><button class="btn sec sm" data-r="${c.id}">Renomear</button> <button class="btn red sm" data-d="${c.id}">Remover</button></td></tr>`).join('') + '</table>';
}
$('bCat').onclick = async () => { await catLista(); $('cErro').style.display = 'none'; $('dCat').showModal(); };
$('cFechar').onclick = () => $('dCat').close();
$('cAdd').onclick = async () => {
  const r = await (await fetch('/api/financeiro/categorias', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({nome: $('cNome').value, tipo: $('cTipo').value})})).json();
  if (!r.ok) { $('cErro').textContent = r.erros.join(' '); $('cErro').style.display = 'block'; return; }
  $('cNome').value = ''; $('cErro').style.display = 'none'; catLista();
};
$('catLista').addEventListener('click', async e => { const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.r) { const c = CATS.find(x => x.id == b.dataset.r), n = prompt('Novo nome da categoria:', c.nome); if (n) await fetch('/api/financeiro/categorias/' + c.id, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({nome: n})}); }
  if (b.dataset.d && confirm('Remover esta categoria? Lançamentos já feitos continuam no histórico.')) await fetch('/api/financeiro/categorias/' + b.dataset.d, {method: 'DELETE'});
  catLista(); lista(); });

function atualizar() { carregar(); lista(); }
$('fMes').onchange = atualizar; $('fAno').onchange = atualizar; $('lCat').onchange = lista; $('lTipo').onchange = lista;
cats().then(atualizar);
</script>
{% endblock %}"""

CLIENTES = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Clientes</h1>
<div class="bar"><form><input name="q" value="{{ q }}" placeholder="Buscar por nome, telefone ou e-mail"><button class="btn sec">Buscar</button></form>
<a class="btn" href="{{ url_for('cliente_form') }}">+ Novo cliente</a></div>
<div class="card">{% if rows %}<table><tr><th>Nome</th><th>Telefone</th><th>E-mail</th><th></th></tr>
{% for c in rows %}<tr><td><a href="{{ url_for('cliente_ver', cid=c.id) }}">{{ c.nome }}</a></td><td>{{ c.telefone }}</td><td>{{ c.email }}</td><td class="n">
<a class="btn sm" href="{{ url_for('cliente_ver', cid=c.id) }}">Ficha</a>
<a class="btn sec sm" href="{{ url_for('cliente_form', cid=c.id) }}">Editar</a>
<form class="inline" method="post" action="{{ url_for('cliente_excluir', cid=c.id) }}" onsubmit="return confirm('Excluir este cliente?')"><button class="btn red sm">Excluir</button></form></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum cliente encontrado.</p>{% endif %}</div>
{% endblock %}"""

CLIENTE_VER = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><h1>{{ c.nome }}</h1>
<div class="bar"><a class="btn" href="{{ url_for('orcamento_form', cliente_id=c.id) }}">+ Novo orçamento</a>
<a class="btn sec" href="{{ url_for('cliente_form', cid=c.id) }}">Editar dados</a></div></div>
<div class="card"><p>{% if c.telefone %}📞 {{ c.telefone }} &nbsp; {% endif %}{% if c.email %}✉ {{ c.email }}{% endif %}{% if c.cidade or c.endereco %}<br>📍 {{ c.endereco or '' }}{% if c.endereco and c.cidade %} — {% endif %}{{ c.cidade or '' }}{% endif %}</p>
{% if c.obs %}<p class="mut">{{ c.obs }}</p>{% endif %}
<p><b>Total em orçamentos aprovados/concluídos:</b> {{ fechado|brl }}</p></div>

<div class="card"><h2>Orçamentos</h2>
{% if orcs %}<table><tr><th>#</th><th>Data</th><th>Peça / descrição</th><th>Situação</th><th class="n">Total</th></tr>
{% for o in orcs %}{% set s = ORC[o.status] %}<tr><td><a href="{{ url_for('orcamento_ver', oid=o.id) }}">{{ o.id }}</a></td><td>{{ o.data|dt }}</td><td>{{ o.titulo or '' }}</td>
<td><span class="tag {{ s[1] }}">{{ s[0] }}</span></td><td class="n">{{ o.total|brl }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum orçamento para este cliente.</p>{% endif %}</div>

<div class="card"><h2>Produtos e serviços orçados / realizados</h2>
{% if linhas %}<table><tr><th>Orç.</th><th>Data</th><th>Produto / composição</th><th class="n">Qtd.</th><th class="n">Total</th><th>Situação</th></tr>
{% for l in linhas %}{% set s = ORC[l.ostatus] %}<tr {% if l.ostatus == 'recusado' %}class="mut"{% endif %}>
<td><a href="{{ url_for('orcamento_ver', oid=l.oid) }}">#{{ l.oid }}</a></td><td>{{ l.odata|dt }}</td>
<td><b>{{ l.nome }}</b>{% if l.largura and l.altura %} <span class="mut">{{ l.largura|num }} × {{ l.altura|num }} cm</span>{% endif %}{% if l.descricao %}<div class="mut">{{ l.descricao }}</div>{% endif %}
<div>{% for c in comps[l.id] %}<span class="tag">{{ c.categoria or c.descricao }} — {{ c.qtd_unit|qtd(c.unidade) }} {{ c.unidade|ul }}</span> {% endfor %}</div></td>
<td class="n">{{ l.qtd }}</td><td class="n">{{ l.total|brl }}</td>
<td><span class="tag {{ s[1] }}">{{ s[0] }}</span></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nada registrado ainda.</p>{% endif %}</div>

{% if vendas %}<div class="card"><h2>Vendas diretas</h2>
<table><tr><th>#</th><th>Data</th><th class="n">Total</th><th></th></tr>
{% for x in vendas %}<tr><td><a href="{{ url_for('venda_ver', vid=x.id) }}">{{ x.id }}</a></td><td>{{ x.data|dt }}</td><td class="n">{{ x.total|brl }}</td><td>{% if x.status == 'cancelada' %}<span class="tag er">cancelada</span>{% endif %}</td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}"""

CLIENTE_FORM = r"""{% extends "base.html" %}{% block conteudo %}
<h1>{{ 'Editar cliente' if cid else 'Novo cliente' }}</h1>
<form class="card" method="post">
<label>Nome *</label><input name="nome" value="{{ c.nome if c else '' }}" required autofocus>
<div class="row2"><div><label>Telefone</label><input name="telefone" value="{{ c.telefone if c else '' }}"></div>
<div><label>E-mail</label><input name="email" type="email" value="{{ c.email if c else '' }}"></div></div>
<div class="row2"><div><label>Cidade</label><input name="cidade" value="{{ c.cidade or '' if c else '' }}"></div>
<div><label>Endereço</label><input name="endereco" value="{{ c.endereco or '' if c else '' }}"></div></div>
<label>Observações</label><textarea name="obs" rows="3">{{ c.obs if c else '' }}</textarea>
<p><button class="btn">Salvar</button> <a class="btn sec" href="{{ url_for('clientes') }}">Cancelar</a></p></form>
{% endblock %}"""

PRODUTOS = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><h1>Produtos e estoque</h1>
<div class="bar noprint" style="margin:0"><a class="btn sec" href="{{ url_for('categorias_produto') }}">Gerenciar categorias</a>
<button type="button" class="btn sec" id="btnCustos" aria-pressed="false"><span id="icoCustos"></span> <span id="txtCustos">Mostrar custos</span></button></div></div>
<div class="grid">
  <div class="card kpi"><small>Investido em estoque (custo)</small><b class="cst" data-k="investido">••••••</b></div>
  <div class="card kpi"><small>Valor de venda do estoque</small><b class="cst" data-k="valor">••••••</b></div>
  <div class="card kpi"><small>Lucro potencial</small><b class="cst" data-k="lucro">••••••</b></div>
</div>
<div id="semCusto" class="flash erro" style="display:none"></div>
<div class="bar"><form><input name="q" value="{{ q }}" placeholder="Buscar por nome ou código"><button class="btn sec">Buscar</button></form>
<a class="btn" href="{{ url_for('produto_form') }}">+ Novo produto</a></div>
<div class="card">{% if rows %}<table><tr><th>Produto</th><th>Categoria</th><th>Código</th><th class="n">Custo</th><th class="n">Preço de venda</th><th class="n">Mínimo</th><th class="n">Estoque</th><th class="n">Investido</th><th></th></tr>
{% for p in rows %}<tr><td>{{ p.nome }}{% if p.servico %} <span class="tag">serviço</span>{% endif %}</td><td>{{ p.categoria or '' }}</td><td>{{ p.sku or '' }}</td>
<td class="n cst" data-id="{{ p.id }}" data-k="custo">••••••</td>
<td class="n">{{ p.preco|brl }}{% if p.unidade != 'un' %} <span class="mut">/ {{ p.unidade|ul }}</span>{% endif %}</td>
<td class="n">{% if p.minimo %}{{ p.minimo|brl }}{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n">{% if not p.servico %}<span class="tag {{ 'er' if p.estoque <= 0 else ('wa' if p.estoque <= p.estoque_min else 'ok') }}">{{ p.estoque|num(4) }} {{ p.unidade|ul }}</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n">{% if not p.servico %}<span class="cst" data-id="{{ p.id }}" data-k="investido">••••••</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n" style="white-space:nowrap"><a class="btn sec sm" href="{{ url_for('produto_form', pid=p.id) }}">Editar</a>
{% if not p.servico %}<a class="btn sec sm" href="{{ url_for('produto_movimentos', pid=p.id) }}">Histórico</a>{% endif %}
<form class="inline" method="post" action="{{ url_for('produto_excluir', pid=p.id) }}" onsubmit="return confirm('Excluir este produto?')"><button class="btn red sm">Excluir</button></form></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum produto cadastrado.</p>{% endif %}</div>
<script>
(function(){
const MASK='••••••', $=id=>document.getElementById(id);
const OLHO_F='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-4px"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
const OLHO_A='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-4px"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
let visivel=false, ocupado=false;
function pinta(){
  $('icoCustos').innerHTML=visivel?OLHO_A:OLHO_F; $('txtCustos').textContent=visivel?'Ocultar custos':'Mostrar custos';
  $('btnCustos').setAttribute('aria-pressed',visivel?'true':'false');
}
function mascara(){document.querySelectorAll('.cst').forEach(e=>{e.textContent=MASK;}); $('semCusto').style.display='none'; $('semCusto').textContent='';}
function preenche(d){
  document.querySelectorAll('.cst').forEach(e=>{
    const k=e.dataset.k, id=e.dataset.id;
    e.textContent=id?((d.produtos[id]||{})[k]||'—'):(d.kpi[k]||'—');
  });
  if(d.sem_custo>0){$('semCusto').textContent=d.sem_custo+' produto(s) com estoque ainda sem preço de custo, então o valor investido está incompleto. Clique em Editar e informe o custo.'; $('semCusto').style.display='';}
}
$('btnCustos').addEventListener('click',async()=>{
  if(ocupado) return;
  if(visivel){visivel=false; mascara(); pinta(); return;}
  ocupado=true;
  try{
    const r=await fetch('/api/produtos/custos',{cache:'no-store'}); if(!r.ok) throw new Error(r.status);
    preenche(await r.json()); visivel=true; pinta();
  }catch(e){alert('Não foi possível carregar os custos agora.');}
  ocupado=false;
});
pinta();
})();
</script>
{% endblock %}"""

PRODUTO_FORM = r"""{% extends "base.html" %}{% block conteudo %}
<h1>{{ 'Editar produto' if pid else 'Novo produto' }}</h1>
<form class="card" method="post">
<div class="row2"><div><label>Nome *</label><input name="nome" value="{{ p.nome if p else '' }}" required autofocus></div>
<div><label>Código / SKU</label><input name="sku" value="{{ p.sku if p else (sku_sug or '') }}">{% if not pid %}<small class="mut">Sugestão automática (contador). Troque por um código seu, se quiser; em branco, o sistema usa o próximo número.</small>{% endif %}</div></div>
<div class="row2"><div><label>Categoria (como aparece nos acompanhamentos do orçamento)</label>
{% set sel = (p.categoria_id|string) if p and p.categoria_id else '' %}
<div style="display:flex;gap:6px"><select name="categoria_id" id="catSel"><option value="">— selecionar categoria —</option>{% for c in cats %}{% if c.ativo or c.id|string == sel %}<option value="{{ c.id }}"{{ ' selected' if c.id|string == sel else '' }}>{{ c.nome }}{{ '' if c.ativo else ' (inativa)' }}</option>{% endif %}{% endfor %}</select>
<button type="button" class="btn sec sm" id="catNova" style="white-space:nowrap">+ Nova categoria</button></div>
<div id="catBox" style="display:none;margin-top:6px;gap:6px"><input id="catNome" placeholder="Nome da nova categoria" maxlength="60" style="flex:1"><button type="button" class="btn sm" id="catSalvar">Cadastrar</button></div>
<div class="hint" id="catMsg"></div></div>
<div id="espbox"><label>Espessura do perfil (cm) <span class="mut">(informativo, não entra no cálculo)</span></label><input name="espessura" inputmode="decimal" value="{{ (p.espessura|num) if p and p.espessura else '' }}" placeholder="ex.: 3"></div></div>
<div class="row2"><div><label>Vendido por</label>
<select name="unidade" id="un">
<option value="un" {{ 'selected' if p and p.unidade == 'un' else '' }}>Unidade (peça pronta, serviço)</option>
<option value="m2" {{ 'selected' if p and p.unidade == 'm2' else '' }}>Metro quadrado (m²), ex.: vidro, paspatur, fundo</option>
<option value="ml" {{ 'selected' if p and p.unidade == 'ml' else '' }}>Metro linear (ML), ex.: moldura em barra</option>
</select></div>
<div><label>Preço de venda (R$) <span id="porun" class="mut">por unidade</span> *</label><input name="preco" inputmode="decimal" value="{{ ('%.2f'|format(p.preco/100)).replace('.', ',') if (p and p.preco is number) else (p.preco if p else '') }}" placeholder="0,00" required></div></div>
<div class="row2"><div><label>Valor mínimo de cobrança (R$) <span class="mut">(opcional, por peça: cobra o maior entre o cálculo e este valor)</span></label><input name="minimo" inputmode="decimal" value="{{ ('%.2f'|format(p.minimo/100)).replace('.', ',') if (p and p.minimo is number) else (p.minimo if p and p.minimo else '') }}" placeholder="ex.: 15,00"></div><div></div></div>
<div style="margin:12px 0 0"><button type="button" class="btn sec sm" id="btnCusto">Mostrar custos</button></div>
<div id="custoBox" style="display:none"><div class="row2"><div><label>Preço de custo (R$) <span class="porun2 mut">por unidade</span></label><input name="custo" inputmode="decimal" value="{{ ('%.2f'|format(p.custo/100)).replace('.', ',') if (p and p.custo is number) else (p.custo if p else '') }}" placeholder="0,00"></div>
<div><label>Lucro sobre o custo</label><div id="margem" style="padding:8px 0">—</div></div></div></div>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="serv" name="servico" {{ 'checked' if p and p.servico and p.servico != '0' else '' }}> É um serviço (ex.: montagem, mão de obra): não controla estoque</label>
<div id="estbox" class="row2"><div><label>Quantidade em estoque (<span class="estun">unidades</span>)</label><input name="estoque" inputmode="decimal" value="{{ (p.estoque|num(4) or '0') if p else '0' }}"></div>
<div><label>Avisar quando o estoque chegar a (<span class="estun">unidades</span>)</label><input name="estoque_min" inputmode="decimal" value="{{ (p.estoque_min|num or '0') if p else '0' }}"></div></div>
<p><button class="btn">Salvar</button> <a class="btn sec" href="{{ url_for('produtos') }}">Cancelar</a></p></form>
<script>
document.getElementById('catNova').onclick = () => { const b = document.getElementById('catBox'); b.style.display = 'flex'; document.getElementById('catNome').focus(); };
document.getElementById('catNome').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('catSalvar').click(); } });
document.getElementById('catSalvar').onclick = async () => {
  const msg = document.getElementById('catMsg'), r = await fetch('/api/categorias-produto', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({nome: document.getElementById('catNome').value})}), j = await r.json();
  if (!j.ok) { msg.textContent = j.erro; msg.style.color = '#b91c1c'; return; }
  const o = new Option(j.nome, j.id, true, true), s = document.getElementById('catSel'); s.add(o); s.value = j.id;
  document.getElementById('catNome').value = ''; document.getElementById('catBox').style.display = 'none'; msg.style.color = ''; msg.textContent = 'Categoria cadastrada e selecionada.';
};
const un=document.getElementById('un'), serv=document.getElementById('serv');
function tog(){
  document.getElementById('estbox').style.display=serv.checked?'none':'';
  document.getElementById('espbox').style.visibility=(un.value==='ml')?'visible':'hidden';
  document.querySelectorAll('.estun').forEach(e=>{e.textContent={un:'unidades',m2:'m²',ml:'metros lineares'}[un.value];});
  const rot={un:'por unidade',m2:'por m²',ml:'por metro linear'}[un.value];
  document.getElementById('porun').textContent=rot;
  document.querySelectorAll('.porun2').forEach(e=>{e.textContent=rot;});
  mg();
}
function n(s){s=(s||'').toString().trim(); if(s.indexOf(',')>=0) s=s.replace(/\./g,'').replace(',','.'); return parseFloat(s)||0;}
function mg(){
  const pr=n(document.querySelector('[name=preco]').value), c=n(document.querySelector('[name=custo]').value), el=document.getElementById('margem');
  if(!c||!pr){el.textContent='—'; return;}
  el.textContent='R$ '+(pr-c).toFixed(2).replace('.',',')+' por '+({un:'unidade',m2:'m²',ml:'metro'}[un.value])+' ('+Math.round((pr-c)/c*100)+'% sobre o custo)';
}
un.addEventListener('change',tog); serv.addEventListener('change',tog); tog();
document.getElementById('btnCusto').addEventListener('click',function(){
  const b=document.getElementById('custoBox'), v=b.style.display==='none';
  b.style.display=v?'':'none'; this.textContent=v?'Ocultar custos':'Mostrar custos';
});
document.querySelector('[name=preco]').addEventListener('input',mg);
document.querySelector('[name=custo]').addEventListener('input',mg);
</script>
{% endblock %}"""

ORCAMENTOS = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Orçamentos</h1>
<div class="bar"><form><input name="q" value="{{ q }}" placeholder="Buscar por cliente, peça ou número">
<select name="status" style="max-width:200px"><option value="">Todas as situações</option>
{% for k, v in ORC.items() %}<option value="{{ k }}" {{ 'selected' if st == k else '' }}>{{ v[0] }}</option>{% endfor %}</select>
<button class="btn sec">Filtrar</button></form>
<a class="btn" href="{{ url_for('orcamento_form') }}">+ Novo orçamento</a></div>
<div class="card">{% if rows %}<table><tr><th>Número</th><th>Cliente</th><th>Data do pedido</th><th>Entrega prevista</th><th class="n">Valor total</th><th>Status</th><th class="n">Ações</th></tr>
{% for o in rows %}{% set s = ORC[o.status] %}<tr><td><a href="{{ url_for('orcamento_ver', oid=o.id) }}">{{ '%03d'|format(o.id) }}</a></td>
<td><a href="{{ url_for('cliente_ver', cid=o.cliente_id) }}">{{ o.cliente }}</a>{% if o.titulo %}<div class="mut" style="font-size:12px">{{ o.titulo }}</div>{% endif %}</td>
<td>{{ o.data|dia }}</td><td>{{ o.entrega|dia or '—' }}</td><td class="n">{{ o.total|brl }}</td>
<td><span class="tag {{ s[1] }}">{{ s[0] }}</span></td>
<td class="n" style="white-space:nowrap"><a class="btn sec sm" href="{{ url_for('orcamento_ver', oid=o.id) }}">Ver</a>
<a class="btn sm" href="{{ url_for('orcamento_form', oid=o.id) }}" title="Editar orçamento">✏️ Editar</a></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum orçamento encontrado.</p>{% endif %}</div>
{% endblock %}"""

ORCAMENTO_FORM = r"""{% extends "base.html" %}{% block head %}<style>
.gcl{display:grid;grid-template-columns:1.5fr 1fr 1fr 1.6fr;gap:0 12px}
.gcl3{display:grid;grid-template-columns:1fr 1fr minmax(0,3fr) 1fr;gap:0 12px}
.gcl2{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,1fr);gap:0 12px}
.gp{display:grid;grid-template-columns:minmax(0,2.6fr) repeat(4,minmax(0,1fr));gap:0 12px}
@media(max-width:1000px){.gcl,.gcl3{grid-template-columns:1fr 1fr}.gcl3>.ref{grid-column:1/-1}.gp{grid-template-columns:1fr 1fr 1fr}.gp>.pn{grid-column:1/-1}}
@media(max-width:560px){.gcl,.gcl2,.gcl3{grid-template-columns:1fr}.gp{grid-template-columns:1fr 1fr}}
.comb{display:flex;gap:6px}.comb select{flex:1;min-width:0}
input[readonly]{background:#f3f5f8;color:#475569}
label{margin:8px 0 3px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}
@media(max-width:1100px){.cols{grid-template-columns:1fr}}
.sticky{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}
@media(max-width:1100px){.sticky{position:static;max-height:none}}
.sec-h{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:14px 0 4px}
.sec-h h2{margin:0}
.ct{border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin-top:8px}
.ct .h,.ct .r{display:grid;grid-template-columns:46px 112px minmax(170px,1.8fr) minmax(88px,.8fr) minmax(112px,.9fr) 86px;gap:8px;align-items:center;padding:7px 12px}
.ct .h{background:#f1f4f8;color:var(--mut);font-size:12px;font-weight:600;padding-top:6px;padding-bottom:6px}
.ct .r{border-top:1px solid var(--bd);background:#fff}
.ct .r:not(.on){background:#fafbfc}
.ct .r:not(.on) .c-nm,.ct .r:not(.on) .c-pr,.ct .r:not(.on) .c-qt{opacity:.6}
.c-nm b{font-size:14px}.c-sub{text-align:right;font-weight:700;white-space:nowrap}
.c-pr{display:flex;align-items:center;gap:4px;white-space:nowrap}.c-pr input{text-align:right;min-width:0}
.ct input,.ct select{padding:5px 8px;font-size:14px}
.sm{font-size:12px}.f-i{margin-top:2px;line-height:1.3}
.warn{color:#c2410c}
.qv{font-weight:600}.qw{display:flex;align-items:center;gap:4px}.qw input{min-width:0;width:78px}
.rst{border:0;background:none;color:var(--pri);cursor:pointer;font-size:12px;padding:0}
input.tg{appearance:none;-webkit-appearance:none;width:38px;height:22px;padding:0;border:0;border-radius:99px;background:#cbd5e1;position:relative;cursor:pointer;transition:background .15s}
input.tg::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
input.tg:checked{background:var(--pri)}input.tg:checked::after{left:18px}
.av-h{margin:14px 0 0;font-size:13px;font-weight:600;color:var(--mut)}
.ct.av{border-style:dashed}
.fixo{padding:5px 0}
@media(max-width:760px){
 .ct .h{display:none}
 .ct .r{grid-template-columns:46px minmax(0,1fr) auto;grid-template-areas:"sw nm sub" "mat mat mat" "qt pr pr"}
 .c-sw{grid-area:sw}.c-nm{grid-area:nm}.c-sub{grid-area:sub}.c-mat{grid-area:mat}.c-qt{grid-area:qt}.c-pr{grid-area:pr}
}
.rs-l{display:flex;justify-content:space-between;gap:8px;padding:5px 0}
.rs-c{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--bd)}
.rs-tot{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;margin-top:8px;border-radius:10px;background:#eaf1ff;font-size:16px;font-weight:600}
.rs-tot b{font-size:24px;color:var(--pri)}
.rs-h{background:#eaf1ff;margin:-16px -16px 12px;padding:12px 16px;border-radius:12px 12px 0 0}.rs-h h2{margin:0;color:#1d4ed8}
ul.erros{margin:10px 0 0;padding-left:18px;color:#b91c1c;font-size:13px}
.cols2{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px}
@media(max-width:900px){.cols2{grid-template-columns:1fr}}
.tl{display:flex;justify-content:space-between;align-items:center;padding:6px 0;gap:10px}
.tl.big{border-top:2px solid var(--bd);margin-top:6px;padding-top:12px;font-size:18px}
.tl.big b{font-size:26px;color:var(--pri)}
table.it td,table.it th{padding:7px 8px}
table.it input.qi{width:64px;padding:4px 6px;text-align:right}
tr.editando td{background:#eff6ff}
tr.det td{background:#f8fafc;border-bottom:1px solid var(--bd)}
tr.det table{font-size:13px;margin:2px 0}
.arrow{cursor:pointer;color:var(--mut);text-decoration:none;display:inline-block;width:16px}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>{{ ('Editar Orçamento #' ~ oid) if oid else 'Novo Orçamento' }}</h1>
<div class="mut">Monte o orçamento adicionando um ou mais produtos configurados.</div></div>
<div class="bar noprint"><a class="btn sec" href="{{ url_for('orcamentos') }}">Cancelar</a><button type="button" class="btn" id="btnSalvarTop">Salvar orçamento</button></div></div>
<div id="alerta" class="flash erro" style="display:none"></div>
{% if estado.baixado %}<div class="flash aviso">Este orçamento está <b>aprovado</b> e já descontou o estoque. Ao salvar, o estoque recebe só o ajuste da diferença.</div>{% endif %}

<div class="card"><h2>Dados do Cliente</h2>
<div class="gcl">
 <div><label for="cliente">Cliente *</label><div class="comb"><select id="cliente"></select><button type="button" class="btn sec" id="btnCli" title="Recarregar a lista de clientes">↻</button></div></div>
 <div><label>Telefone</label><input id="cli_tel" readonly placeholder="—"></div>
 <div><label>Cidade</label><input id="cli_cid" readonly placeholder="—"></div>
 <div><label>Endereço</label><input id="cli_end" readonly placeholder="—"></div>
 </div>
<div class="gcl3">
 <div><label for="data">Data do pedido</label><input type="date" id="data"></div>
 <div><label for="entrega">Data prevista de entrega</label><input type="date" id="entrega"></div>
 <div><label for="vendedor">Vendedor responsável</label><input id="vendedor" maxlength="80" placeholder="Nome do vendedor"></div>
 <div class="ref"><label for="titulo">Referência do orçamento (opcional)</label><input id="titulo" placeholder="Ex.: Quadros da sala de estar" maxlength="200"></div>
 <div><label for="perda_pad" title="Perda de corte da moldura, usada como padrão em cada produto novo">Perda padrão moldura (cm)</label><input id="perda_pad" inputmode="decimal"></div>
</div>
<div class="hint"><a href="{{ url_for('cliente_form') }}" target="_blank">+ Cadastrar novo cliente</a> (abre em outra aba; depois clique em ↻) · <a href="#" id="cli_edit" target="_blank" style="display:none">editar cadastro</a></div>
</div>

<div class="cols">
 <div class="card"><h2 id="cfgTitulo">Configurar Produto</h2>
  <div class="gp">
   <div class="pn"><label for="p_nome">Produto / Serviço *</label><input id="p_nome" list="dl_nomes" placeholder="Ex.: Quadro, Espelho..." maxlength="120" autocomplete="off">
    <datalist id="dl_nomes">{% for n in nomes %}<option value="{{ n }}">{% endfor %}</datalist></div>
   <div><label for="p_l">Largura (cm) *</label><input id="p_l" inputmode="decimal" placeholder="ex.: 25"></div>
   <div><label for="p_a">Altura (cm) *</label><input id="p_a" inputmode="decimal" placeholder="ex.: 30"></div>
   <div><label for="p_qtd">Quantidade *</label><input id="p_qtd" type="number" min="1" step="1" value="1"></div>
   <div><label for="p_perda" title="Perda de corte desta peça. Não é a espessura da moldura.">Perda moldura (cm)</label><input id="p_perda" inputmode="decimal"></div>
  </div>
  <label for="p_desc">Descrição / observações deste produto (opcional)</label><input id="p_desc" maxlength="500" placeholder="Ex.: moldura preta, com paspatur branco">

  <div class="sec-h"><h2>Acompanhamentos</h2><button type="button" class="btn sec sm" id="btnPrecos" style="display:none">↻ Usar preços atuais do cadastro</button></div>
  <div class="hint" style="margin:0">Ative o que este produto leva. A largura e a altura acima valem para todos os componentes. O preço vale só para este orçamento: o cadastro não é alterado.</div>
  {% if not cat %}<div class="flash erro" style="margin-top:8px">Nenhum produto cadastrado. Cadastre os materiais em <a href="{{ url_for('produto_form') }}" target="_blank">Produtos</a> (informe a categoria: Moldura, Vidro, Fundo...) e recarregue esta página. Você também pode usar “+ Componente avulso”.</div>{% endif %}
  <div class="ct"><div class="h"><span></span><span>Componente</span><span>Material / Variação</span><span>Quantidade</span><span>Preço</span><span style="text-align:right">Subtotal</span></div><div id="comps"></div></div>
  <div id="avBox" style="display:none"><div class="av-h">Componentes avulsos</div><div class="ct av"><div id="avulsos"></div></div></div>
  <div style="margin-top:10px"><button type="button" class="btn sec sm" id="btnAvulso">+ Componente avulso</button></div>
  <div id="dls"></div>
 </div>

 <div class="sticky"><div class="card" id="resumo"><div class="rs-h"><h2>Resumo do Cálculo</h2></div><div id="rs"></div>
  <button type="button" class="btn" id="btnAdd" style="width:100%;margin-top:12px;padding:11px">+ Adicionar ao orçamento</button>
  <button type="button" class="btn sec" id="btnCancEdit" style="width:100%;margin-top:6px;display:none">Cancelar edição do item</button></div></div>
</div>

<div class="card"><div class="sec-h" style="margin-top:0"><h2>Itens do Orçamento</h2><button type="button" class="btn sec sm" id="btnLimpar">Limpar itens</button></div>
 <div id="tabela"></div></div>

<div class="card"><div class="cols2">
 <div><label for="prazo">Prazo de entrega</label><input id="prazo" placeholder="Ex.: 7 dias úteis" maxlength="200">
  <label for="obs">Observações</label><textarea id="obs" rows="4" maxlength="2000"></textarea></div>
 <div>
  <div class="tl"><span>Subtotal dos produtos</span><b id="t_sub">R$ 0,00</b></div>
  <div class="tl"><label for="desc_tipo" style="margin:0;color:inherit;font-size:inherit">Tipo de desconto</label><select id="desc_tipo" style="max-width:150px"><option value="valor">R$ (valor fixo)</option><option value="pct">% (percentual)</option></select></div>
  <div class="tl"><label for="desconto" id="desc_rot" style="margin:0;color:inherit;font-size:inherit">Valor do desconto (R$)</label><input id="desconto" inputmode="decimal" style="max-width:150px;text-align:right"></div>
  <div class="tl"><span class="mut">Desconto aplicado</span><b id="t_desc">− R$ 0,00</b></div>
  <div id="desc_err" class="sm" style="color:#b91c1c;text-align:right;min-height:0"></div>
  <div class="tl big"><span>Valor total</span><b id="t_tot">R$ 0,00</b></div>
  <div class="bar noprint" style="margin:12px 0 0"><button type="button" class="btn" id="btnSalvar">Salvar orçamento</button><a class="btn sec" href="{{ url_for('orcamentos') }}">Cancelar</a></div>
 </div></div></div>

<script>
(function(){
'use strict';
const CAT={{ cat|tojson }}, EST={{ estado|tojson }};
let clientes={{ clientes|tojson }};
const UL={un:'un',m2:'m²',ml:'ML'}, UN_TXT={un:'Unidade',m2:'Metro quadrado',ml:'Metro linear'};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function brl(c){c=Math.round(c||0);const neg=c<0;c=Math.abs(c);return (neg?'-':'')+'R$ '+String(Math.floor(c/100)).replace(/\B(?=(\d{3})+(?!\d))/g,'.')+','+String(c%100).padStart(2,'0');}
function cents(t){t=String(t==null?'':t).replace(/R\$|\s/g,'');if(t.indexOf(',')>=0)t=t.replace(/\./g,'').replace(',','.');const n=parseFloat(t);return isNaN(n)?0:Math.round(n*100);}
function reais(c){return (c/100).toFixed(2).replace('.',',');}
const fq=n=>Number(n).toLocaleString('pt-BR',{maximumFractionDigits:4});
const catDe=nome=>CAT.find(g=>g.nome===nome);
function prodDe(id){for(const g of CAT){const p=g.produtos.find(x=>x.id===id);if(p)return p;}return null;}
/* nome mostrado na lista suspensa (acrescenta o código só se houver nomes repetidos na categoria) */
CAT.forEach(g=>{const n={};g.produtos.forEach(p=>{n[p.nome]=(n[p.nome]||0)+1;});g.produtos.forEach(p=>{p.rot=n[p.nome]>1?(p.nome+' (#'+p.id+')'):p.nome;});});
const achaMat=(g,txt)=>g?g.produtos.find(p=>p.rot===txt.trim()):null;

let linhas=EST.linhas.slice(), cfg=null, editIdx=-1, dirty=false, seq=0, timer=null, busy=false, nCustom=1000, salvando=false, salvo=false;
const abertos=new Set();

function novoComp(o){return Object.assign({key:'',categoria:'',ativo:false,custom:false,produto_id:null,nome:'',unidade:'',qtd:'',manual:false,preco:'',espessura:'',minimo:''},o||{});}
function novoCfg(){return {comps:CAT.map(g=>novoComp({key:'c:'+g.nome,categoria:g.nome}))};}
function novoCustom(){return novoComp({key:'x:'+(++nCustom),ativo:true,custom:true});}
function cfgDe(inp){
  const c=novoCfg();
  (inp.componentes||[]).forEach(s=>{
    let r=s.custom?null:c.comps.find(x=>x.key===s.key);
    if(!r){r=novoCustom();r.key=s.key||r.key;c.comps.push(r);}
    Object.assign(r,{ativo:true,custom:!!s.custom||r.custom,categoria:s.categoria||r.categoria||'',produto_id:s.produto_id||null,nome:s.nome||'',unidade:s.unidade||'',qtd:s.qtd||'',manual:!!s.manual,preco:s.preco||'',espessura:s.espessura||'',minimo:s.minimo||''});
  });
  return c;
}
function preencheForm(inp){
  $('p_nome').value=inp?inp.nome:''; $('p_desc').value=inp?inp.descricao:'';
  $('p_l').value=inp?inp.largura:''; $('p_a').value=inp?inp.altura:'';
  $('p_qtd').value=inp?inp.qtd:'1'; $('p_perda').value=inp?inp.perda:($('perda_pad').value||'0');
}
function payloadDe(inp,qtd){return Object.assign({},inp,{qtd:qtd});}
function payload(){
  return {arred:true,nome:$('p_nome').value,descricao:$('p_desc').value,largura:$('p_l').value,altura:$('p_a').value,qtd:$('p_qtd').value,perda:$('p_perda').value,
    componentes:cfg.comps.filter(r=>r.ativo).map(r=>({key:r.key,ativo:true,custom:r.custom,produto_id:r.produto_id,categoria:r.categoria,nome:r.nome,unidade:r.unidade,manual:r.manual,qtd:r.qtd,preco:r.preco,espessura:r.espessura,minimo:r.minimo}))};
}
function tocado(){return !!($('p_nome').value.trim()||$('p_l').value.trim()||$('p_a').value.trim()||cfg.comps.some(r=>r.ativo)||editIdx>=0);}
function sujo(){dirty=true;}

/* ---------- clientes ---------- */
function montaClientes(sel){
  const s=$('cliente'), atual=sel!=null?String(sel):s.value;
  s.innerHTML='<option value="">Selecione o cliente...</option>'+clientes.map(c=>'<option value="'+c.id+'">'+esc(c.nome)+'</option>').join('');
  s.value=atual; if(s.value!==atual) s.value='';
  preencheCliente();
}
function preencheCliente(){
  const c=clientes.find(x=>String(x.id)===$('cliente').value);
  $('cli_tel').value=c?c.telefone:''; $('cli_cid').value=c?c.cidade:''; $('cli_end').value=c?c.endereco:'';
  const a=$('cli_edit'); a.style.display=c?'':'none'; a.href=c?('/clientes/'+c.id+'/editar'):'#';
}

/* ---------- tabela de acompanhamentos ---------- */
function listas(){
  $('dls').innerHTML=CAT.map((g,i)=>'<datalist id="dl_'+i+'">'+g.produtos.map(p=>'<option value="'+esc(p.rot)+'">'+esc(brl(p.preco)+(p.un!=='un'?' / '+UL[p.un]:'')+(p.controla?' · estoque '+fq(p.estoque):''))+'</option>').join('')+'</datalist>').join('');
}
function linhaHTML(r){
  const idx=CAT.findIndex(g=>g.nome===r.categoria);
  let mat;
  if(!r.custom) mat='<input class="mat" list="dl_'+idx+'" placeholder="Selecione o material..." autocomplete="off">';
  else if(r.produto_id) mat='<div class="fixo mat-fixo"></div>';
  else mat='<div class="comb"><input class="cnome" placeholder="Descrição (ex.: Mão de obra)" maxlength="120"><select class="cun" style="max-width:104px"><option value="">Unidade</option><option value="un">unidade</option><option value="m2">m²</option><option value="ml">ML</option></select></div>';
  return '<div class="c-sw"><input type="checkbox" class="tg ck" aria-label="Ativar"></div>'+
   '<div class="c-nm"><b class="nm"></b><div class="sm mut inf"></div></div>'+
   '<div class="c-mat">'+mat+'<div class="f-i sm mut"></div></div>'+
   '<div class="c-qt"><span class="qv">—</span><span class="qw" style="display:none"><input class="cq" inputmode="decimal"><span class="qu sm mut"></span><button type="button" class="rst" title="Voltar ao cálculo automático">↺</button></span></div>'+
   '<div class="c-pr"><input class="cp" inputmode="decimal" style="width:78px"><span class="porun sm mut"></span></div>'+
   '<div class="c-sub"><span class="svt">R$ 0,00</span></div>';
}
function renderComps(){
  const box=$('comps'), av=$('avulsos'); box.innerHTML=''; av.innerHTML='';
  cfg.comps.forEach(r=>{
    const el=document.createElement('div'); el.className='r'; el.dataset.key=r.key; el.innerHTML=linhaHTML(r);
    if(r.custom){
      const rm=document.createElement('button'); rm.type='button'; rm.className='btn red sm rm'; rm.title='Remover componente'; rm.textContent='×'; rm.style.marginLeft='4px';
      el.querySelector('.c-sub').appendChild(rm);
      av.appendChild(el);
    } else box.appendChild(el);
    liga(el,r); sinc(el,r);
  });
  $('avBox').style.display=cfg.comps.some(r=>r.custom)?'':'none';
}
function escolhe(r,p){
  r.produto_id=p?p.id:null; r.unidade=p?p.un:''; r.manual=false;
  r.minimo=(p&&p.minimo)?reais(p.minimo):''; r.preco=p?reais(p.preco):''; r.espessura=(p&&p.un==='ml')?(p.espessura||''):''; r.qtd=(p&&p.un==='un')?'1':'';
}
function sinc(el,r){
  const q=s=>el.querySelector(s), p=r.produto_id?prodDe(r.produto_id):null, u=r.unidade;
  el.classList.toggle('on',r.ativo); q('.ck').checked=r.ativo;
  q('.nm').textContent=r.custom?(r.categoria||(r.produto_id?'Outros':'Avulso')):r.categoria;
  q('.inf').textContent=u?(UN_TXT[u]+(u==='ml'&&r.espessura?' · esp. '+r.espessura+' cm':'')):'';
  const m=q('.mat'); if(m&&document.activeElement!==m) m.value=p?p.rot:'';
  if(q('.mat-fixo')) q('.mat-fixo').textContent=r.nome;
  if(q('.cnome')){ if(document.activeElement!==q('.cnome')) q('.cnome').value=r.nome; q('.cun').value=u; }
  const entra=(u==='un'||r.manual);
  q('.qv').style.display=entra?'none':''; q('.qw').style.display=entra?'':'none';
  if(entra&&document.activeElement!==q('.cq')) q('.cq').value=r.qtd;
  q('.qu').textContent=u?UL[u]:''; q('.rst').style.display=(r.manual&&u!=='un')?'':'none';
  if(!entra&&!u) q('.qv').textContent='—';
  if(document.activeElement!==q('.cp')) q('.cp').value=r.preco;
  q('.porun').textContent=u?('/ '+UL[u]):'';
  q('.cq').disabled=q('.cp').disabled=!r.ativo;
}
function liga(el,r){
  const q=s=>el.querySelector(s);
  q('.ck').addEventListener('change',e=>{
    r.ativo=e.target.checked;
    if(r.ativo&&!r.custom&&!r.produto_id){const g=catDe(r.categoria); if(g&&g.produtos.length===1) escolhe(r,g.produtos[0]);}
    sinc(el,r); agenda();
  });
  if(q('.mat')){
    q('.mat').addEventListener('input',e=>{
      const g=catDe(r.categoria), p=achaMat(g,e.target.value);
      if(p){escolhe(r,p); r.ativo=true; sinc(el,r); agenda();}
      else if(e.target.value.trim()===''&&r.produto_id){escolhe(r,null); r.ativo=false; sinc(el,r); agenda();}
    });
    q('.mat').addEventListener('change',e=>{sinc(el,r);});
    q('.mat').addEventListener('blur',e=>{sinc(el,r);});
  }
  if(q('.cnome')){
    q('.cnome').addEventListener('input',e=>{r.nome=e.target.value; agenda();});
    q('.cun').addEventListener('change',e=>{r.unidade=e.target.value; if(r.unidade==='un'&&!r.qtd) r.qtd='1'; sinc(el,r); agenda();});
  }
  q('.cq').addEventListener('input',e=>{r.qtd=e.target.value; if(r.unidade!=='un'){r.manual=true; q('.rst').style.display='';} agenda();});
  q('.rst').addEventListener('click',()=>{r.manual=false; r.qtd=''; sinc(el,r); agenda(true);});
  q('.cp').addEventListener('input',e=>{r.preco=e.target.value; agenda();});
  if(q('.rm')) q('.rm').addEventListener('click',()=>{cfg.comps=cfg.comps.filter(x=>x!==r); renderComps(); agenda();});
}

/* ---------- cálculo (feito no servidor) ---------- */
function agenda(rapido){sujo(); clearTimeout(timer); timer=setTimeout(calcular,rapido?0:150);}
async function calcular(){
  const meu=++seq; let j;
  try{
    const resp=await fetch('/api/orcamentos/calcular',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});
    j=await resp.json();
  }catch(e){ $('rs').innerHTML='<p class="warn">Não foi possível calcular agora ('+esc(e)+').</p>'; return null; }
  if(meu!==seq) return null;
  aplica(j); return j;
}
function aplica(j){
  const mapa={}; (j.linha.componentes||[]).forEach(c=>{if(c.key) mapa[c.key]=c;});
  let difere=false;
  document.querySelectorAll('#comps .r,#avulsos .r').forEach(el=>{
    const r=cfg.comps.find(x=>x.key===el.dataset.key); if(!r) return;
    const c=mapa[r.key], q=s=>el.querySelector(s), p=r.produto_id?prodDe(r.produto_id):null;
    q('.svt').textContent=(r.ativo&&c)?c.sub_unit_txt:'R$ 0,00';
    if(r.unidade!=='un'&&!r.manual) q('.qv').textContent=(r.ativo&&c)?(c.qtd_unit_txt+' '+c.rotulo_un):'—';
    let h='';
    if(p&&p.controla){
      h+='Estoque: '+fq(p.estoque)+' '+UL[p.un];
      if(r.ativo&&c&&!EST.baixado&&c.qtd_total>p.estoque+0.0005) h+=' <span class="warn">· insuficiente (não bloqueia)</span>';
    }
    if(r.ativo&&c&&c.minimo_aplicado) h+=(h?' · ':'')+'Mínimo aplicado: '+esc(c.minimo_txt)+' (cálculo: '+esc(c.calc_unit_txt)+')';
    if(r.ativo&&p&&((r.preco&&cents(r.preco)!==p.preco)||(r.minimo?cents(r.minimo):0)!==(p.minimo||0))){
      difere=true; h+=(h?' · ':'')+'Cadastro hoje: '+brl(p.preco)+(p.minimo?' (mín. '+brl(p.minimo)+')':'')+' <a href="#" class="usar">usar</a>';
    }
    q('.f-i').innerHTML=h;
    const u=q('.usar'); if(u) u.onclick=ev=>{ev.preventDefault(); r.preco=reais(p.preco); r.minimo=p.minimo?reais(p.minimo):''; sinc(el,r); agenda(true);};
  });
  $('btnPrecos').style.display=difere?'':'none';
  resumo(j);
}
function resumo(j){
  const el=$('rs'), L=j.linha;
  if(!tocado()){el.innerHTML='<p class="mut">Informe o produto, a largura e a altura e ative os acompanhamentos para ver o cálculo.</p>'; return;}
  let h='<div style="font-size:16px"><b>'+esc(L.nome||'(sem nome)')+'</b></div>';
  h+='<div class="sm mut">Medidas: '+esc(L.medidas||'—')+(L.base_calc?'<br>Base de preço: '+esc(L.base_calc)+' (múltiplo de 5)':'')+'<br>Quantidade: '+L.qtd+'</div><div style="margin-top:8px">';
  h+=L.componentes.map(c=>'<div class="rs-c"><div><b>'+esc(c.categoria||c.descricao)+'</b>'+(c.categoria&&c.descricao&&c.descricao!==c.categoria?' <span class="mut">· '+esc(c.descricao)+'</span>':'')+'<div class="sm mut">'+esc(c.qtd_unit_txt)+' '+esc(c.rotulo_un)+' × '+esc(c.preco_unit_txt)+'</div></div><b>'+esc(c.sub_unit_txt)+'</b></div>').join('');
  h+='</div><div class="rs-l"><span>Valor unitário</span><b>'+esc(L.valor_unit_txt)+'</b></div>';
  h+='<div class="rs-l"><span>Quantidade</span><b>'+L.qtd+'</b></div>';
  h+='<div class="rs-tot"><span>Total do item</span><b>'+esc(L.total_txt)+'</b></div>';
  if(j.erros.length) h+='<ul class="erros">'+j.erros.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ul>';
  el.innerHTML=h;
}

/* ---------- adicionar / editar / excluir ---------- */
function mostraErro(msgs){
  const a=$('alerta'); if(!msgs||!msgs.length){a.style.display='none'; return;}
  a.innerHTML=msgs.map(m=>'<div>'+esc(m)+'</div>').join(''); a.style.display='';
  window.scrollTo({top:0,behavior:'smooth'});
}
function resetCfg(){editIdx=-1; cfg=novoCfg(); preencheForm(null); renderComps(); $('cfgTitulo').textContent='Configurar Produto'; $('btnAdd').textContent='+ Adicionar ao orçamento'; $('btnCancEdit').style.display='none'; calcular();}
async function adicionar(){
  if(busy) return; busy=true; $('btnAdd').disabled=true; mostraErro(null);
  try{
    let j=null; for(let k=0;k<4&&!j;k++) j=await calcular();
    if(!j){alert('Não foi possível calcular. Tente novamente.'); return;}
    if(!j.ok){resumo(j); return;}
    const item={inputs:payload(),view:j.linha};
    if(editIdx>=0) linhas[editIdx]=item; else linhas.push(item);
    sujo(); renderTabela(); resetCfg(); $('p_nome').focus();
  } finally {busy=false; $('btnAdd').disabled=false;}
}
function editar(i){
  const it=linhas[i]; editIdx=i; cfg=cfgDe(it.inputs); preencheForm(it.inputs); renderComps();
  $('cfgTitulo').textContent='Editando item '+(i+1); $('btnAdd').textContent='✓ Salvar alterações do item'; $('btnCancEdit').style.display='';
  renderTabela(); calcular(); $('cfgTitulo').scrollIntoView({behavior:'smooth',block:'start'});
}
async function mudaQtd(i,val){
  const n=Number(val), it=linhas[i];
  if(!Number.isInteger(n)||n<1){renderTabela(); return;}
  const inp=payloadDe(it.inputs,String(n));
  try{
    const j=await (await fetch('/api/orcamentos/calcular',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(inp)})).json();
    if(j.ok){linhas[i]={inputs:inp,view:j.linha}; if(editIdx===i) $('p_qtd').value=String(n); sujo();}
    else mostraErro(j.erros);
  }catch(e){mostraErro(['Não foi possível recalcular: '+e]);}
  renderTabela();
}
function renderTabela(){
  const box=$('tabela');
  if(!linhas.length){box.innerHTML='<p class="mut">Nenhum produto adicionado ainda. Configure um produto acima e clique em “+ Adicionar ao orçamento”.</p>';}
  else{
    box.innerHTML='<table class="it"><thead><tr><th>#</th><th>Produto / Serviço</th><th>Medidas</th><th>Componentes</th><th class="n">Qtd.</th><th class="n">Valor unit.</th><th class="n">Total</th><th></th></tr></thead><tbody>'+
    linhas.map((it,i)=>{
      const v=it.view, ab=abertos.has(i);
      const comp=v.componentes.map(c=>esc(c.descricao||c.categoria)).join(' + ');
      let r='<tr class="'+(i===editIdx?'editando':'')+'"><td>'+(i+1)+'</td><td><a href="#" class="arrow" data-det="'+i+'" title="Ver composição">'+(ab?'▾':'▸')+'</a><b>'+esc(v.nome)+'</b>'+(v.descricao?'<div class="sm mut" style="margin-left:16px">'+esc(v.descricao)+'</div>':'')+'</td><td>'+esc(v.medidas||'—')+'</td><td>'+comp+'</td><td class="n"><input type="number" class="qi" min="1" step="1" value="'+v.qtd+'" data-q="'+i+'" aria-label="Quantidade"></td><td class="n">'+esc(v.valor_unit_txt)+'</td><td class="n"><b>'+esc(v.total_txt)+'</b></td><td class="n" style="white-space:nowrap"><button type="button" class="btn sec sm" data-ed="'+i+'">Editar</button> <button type="button" class="btn red sm" data-del="'+i+'">Excluir</button></td></tr>';
      if(ab) r+='<tr class="det"><td></td><td colspan="7"><table><tr><th>Componente</th><th>Material</th><th class="n">Qtd. por peça</th><th class="n">Preço aplicado</th><th class="n">Subtotal por peça</th></tr>'+v.componentes.map(c=>'<tr><td>'+esc(c.categoria||'Avulso')+'</td><td>'+esc(c.descricao)+'</td><td class="n">'+esc(c.qtd_unit_txt)+' '+esc(c.rotulo_un)+'</td><td class="n">'+esc(c.preco_unit_txt)+(c.unidade!=='un'?' / '+esc(c.rotulo_un):'')+'</td><td class="n">'+esc(c.sub_unit_txt)+'</td></tr>').join('')+'</table></td></tr>';
      return r;
    }).join('')+'</tbody></table>';
  }
  totais();
}
function numTxt(t){t=String(t==null?'':t).replace(/R\$|%|\s/g,'');if(t.indexOf(',')>=0)t=t.replace(/\./g,'').replace(',','.');return parseFloat(t);}
function descAplicado(sub,piso){
  const tipo=$('desc_tipo').value, t=$('desconto').value.trim();
  if(!t) return {ap:0,erro:''};
  const n=numTxt(t);
  if(isNaN(n)||n<0) return {ap:0,erro:'Informe um número válido, sem sinal negativo.'};
  if(tipo==='pct'){
    if(n>100) return {ap:0,erro:'O percentual não pode passar de 100%.'};
    const bp=Math.round(n*100), ap=Math.floor((sub*bp+5000)/10000);
    return ap>sub-piso?{ap:0,erro:'Desconto máximo permitido: '+brl(sub-piso)+' (valores mínimos preservados).'}:{ap:ap,erro:''};
  }
  const ap=Math.round(n*100); return ap>sub-piso?{ap:0,erro:(piso>0?'Desconto máximo permitido: '+brl(sub-piso)+' (valores mínimos preservados).':'O desconto é maior que o subtotal.')}:{ap:ap,erro:''};
}
function totais(){
  const sub=linhas.reduce((s,it)=>s+it.view.total,0), d=descAplicado(sub,linhas.reduce((s,it)=>s+(it.view.piso||0),0)), tot=sub-d.ap;
  $('t_sub').textContent=brl(sub); $('t_desc').textContent='− '+brl(d.ap); $('desc_err').textContent=d.erro;
  $('t_tot').textContent=brl(tot); $('t_tot').style.color=tot<0?'#dc2626':'';
}
function rotDesc(){$('desc_rot').textContent=$('desc_tipo').value==='pct'?'Valor do desconto (%)':'Valor do desconto (R$)';}

/* ---------- salvar ---------- */
async function salvar(){
  if(salvando) return; mostraErro(null);
  if(tocado()&&!confirm('Há um produto em configuração que ainda não foi adicionado (ou uma edição não aplicada). Salvar mesmo assim, sem ele?')) return;
  if(!linhas.length){mostraErro(['Adicione ao menos um produto ao orçamento.']); return;}
  if(!$('cliente').value){mostraErro(['Escolha o cliente do orçamento.']); $('cliente').focus(); return;}
  salvando=true; ['btnSalvar','btnSalvarTop'].forEach(i=>$(i).disabled=true);
  try{
    const resp=await fetch('/api/orcamentos/salvar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({oid:EST.oid,token:EST.token,cliente_id:$('cliente').value,data:$('data').value,entrega:$('entrega').value,vendedor:$('vendedor').value,titulo:$('titulo').value,perda:$('perda_pad').value,desconto_tipo:$('desc_tipo').value,desconto:$('desconto').value,prazo:$('prazo').value,obs:$('obs').value,linhas:linhas.map(l=>l.inputs)})});
    const j=await resp.json();
    if(j.ok){salvo=true; location.href=j.url; return;}
    mostraErro(j.erros||['Não foi possível salvar.']);
  }catch(e){mostraErro(['Falha ao salvar: '+e]);}
  salvando=false; ['btnSalvar','btnSalvarTop'].forEach(i=>$(i).disabled=false);
}

/* ---------- início ---------- */
$('perda_pad').value=EST.perda; $('data').value=EST.data; $('titulo').value=EST.titulo; $('desc_tipo').value=EST.desconto_tipo||'valor'; rotDesc(); $('entrega').value=EST.entrega||''; $('vendedor').value=EST.vendedor||''; $('desconto').value=EST.desconto; $('prazo').value=EST.prazo; $('obs').value=EST.obs;
listas(); cfg=novoCfg(); preencheForm(null); montaClientes(EST.cliente_id||''); renderComps(); renderTabela(); calcular();
$('cliente').addEventListener('change',()=>{preencheCliente(); sujo();});
$('btnCli').addEventListener('click',async()=>{try{clientes=await (await fetch('/api/clientes')).json(); montaClientes();}catch(e){alert('Não foi possível recarregar os clientes.');}});
['p_nome','p_desc','p_qtd','p_l','p_a','p_perda'].forEach(id=>$(id).addEventListener('input',()=>agenda()));
$('perda_pad').addEventListener('input',()=>{$('p_perda').value=$('perda_pad').value; agenda();});
['titulo','prazo','obs','data'].forEach(id=>$(id).addEventListener('input',sujo));
$('desconto').addEventListener('input',()=>{totais(); sujo();});
$('desc_tipo').addEventListener('change',()=>{$('desconto').value=''; rotDesc(); totais(); sujo();});
$('entrega').addEventListener('input',sujo); $('vendedor').addEventListener('input',sujo);
$('btnAvulso').addEventListener('click',()=>{cfg.comps.push(novoCustom()); renderComps(); agenda();});
$('btnPrecos').addEventListener('click',()=>{cfg.comps.forEach(r=>{const p=r.ativo&&r.produto_id?prodDe(r.produto_id):null; if(p){r.preco=reais(p.preco); r.minimo=p.minimo?reais(p.minimo):'';}}); renderComps(); agenda(true);});
$('btnAdd').addEventListener('click',adicionar);
$('btnCancEdit').addEventListener('click',()=>{resetCfg(); renderTabela();});
$('btnLimpar').addEventListener('click',()=>{if(!linhas.length) return; if(confirm('Remover todos os itens deste orçamento?')){linhas=[]; abertos.clear(); editIdx=-1; sujo(); renderTabela(); resetCfg();}});
$('tabela').addEventListener('click',e=>{
  const t=e.target;
  if(t.matches('[data-ed]')) editar(parseInt(t.dataset.ed,10));
  else if(t.matches('[data-del]')){const i=parseInt(t.dataset.del,10); if(confirm('Excluir este item do orçamento?')){linhas.splice(i,1); abertos.clear(); if(editIdx===i){resetCfg();} else if(editIdx>i){editIdx--;} sujo(); renderTabela();}}
  else if(t.matches('[data-det]')){e.preventDefault(); const i=parseInt(t.dataset.det,10); if(abertos.has(i)) abertos.delete(i); else abertos.add(i); renderTabela();}
});
$('tabela').addEventListener('change',e=>{if(e.target.matches('[data-q]')) mudaQtd(parseInt(e.target.dataset.q,10),e.target.value);});
$('btnSalvar').addEventListener('click',salvar); $('btnSalvarTop').addEventListener('click',salvar);
window.addEventListener('beforeunload',e=>{if(dirty&&!salvo){e.preventDefault(); e.returnValue='';}});
dirty=false;
})();
</script>
{% endblock %}"""

ORCAMENTO_VER = r"""{% extends "base.html" %}{% block head %}<style>
.cabdoc{display:flex;gap:20px;align-items:center;justify-content:space-between;border-bottom:2px solid var(--tx);padding-bottom:10px;margin-bottom:10px}
.cabdoc .logo{flex:0 0 190px;max-width:40%}.cabdoc .logo img{max-width:100%;max-height:90px;display:block}
.cabdoc .dados{text-align:right;font-size:13px;line-height:1.45}.cabdoc .nome{font-size:19px;font-weight:700}
.tdoc{font-size:17px;font-weight:700;letter-spacing:.03em;margin:4px 0 8px}
.rodape{margin-top:18px;padding-top:8px;border-top:1px solid var(--bd);font-size:12px;color:var(--mut)}
@media print{@page{size:A4;margin:14mm}body{font-size:12px}.cab h1{display:none}.cabdoc{border-color:#000}a{color:inherit;text-decoration:none}}
</style>{% endblock %}{% block conteudo %}
{% set s = ORC[o.status] %}
<div class="cab"><h1>Orçamento #{{ o.id }} <span class="tag {{ s[1] }} noprint">{{ s[0] }}</span> {% if o.estoque_baixado %}<span class="tag noprint">estoque descontado</span>{% endif %}</h1>
<div class="bar noprint"><a class="btn sec" target="_blank" href="{{ url_for('orcamento_imprimir', oid=o.id, modo='orc', auto=1) }}">🖨 Imprimir orçamento</a>
<a class="btn sec" target="_blank" href="{{ url_for('orcamento_imprimir', oid=o.id, modo='2vias', auto=1) }}">🖨 Imprimir 2 vias</a>
{% if o.status in ('aprovado', 'producao', 'concluido') %}<a class="btn sec" target="_blank" href="{{ url_for('orcamento_imprimir', oid=o.id, modo='os', auto=1) }}">🖨 Imprimir ordem de serviço</a>{% endif %}
<a class="btn sec" href="{{ url_for('orcamento_form', oid=o.id) }}">Editar</a>
<form class="inline" method="post" action="{{ url_for('orcamento_duplicar', oid=o.id) }}"><button class="btn sec">Duplicar</button></form>
{% if o.status == 'concluido' %}<form class="inline" method="post" action="{{ url_for('avaliacao_enviar', oid=o.id) }}" target="_blank"><button class="btn" style="background:#16a34a">💬 {{ 'Reenviar pedido de avaliação' if o.aval_enviada_em else 'Pedir avaliação no WhatsApp' }}</button></form>{% if o.aval_enviada_em %}<span class="tag ok">avaliação pedida em {{ o.aval_enviada_em|dt }}</span>{% endif %}{% endif %}</div></div>

<div class="card">
<div class="cabdoc"><div class="logo">{% if emp.tem_logo %}<img src="{{ url_for('empresa_logo') }}?v={{ emp.versao }}" alt="">{% endif %}</div>
<div class="dados"><div class="nome">{{ emp.nome }}</div>
{% if emp.razao_extra %}<div>{{ emp.razao_extra }}</div>{% endif %}
{% if emp.cnpj %}<div>CNPJ: {{ emp.cnpj }}</div>{% endif %}
{% if emp.linha_rua %}<div>{{ emp.linha_rua }}</div>{% endif %}
{% if emp.linha_cidade %}<div>{{ emp.linha_cidade }}</div>{% endif %}
{% if emp.fones %}<div>{{ emp.fones }}</div>{% endif %}
{% if emp.email %}<div>{{ emp.email }}</div>{% endif %}
{% if emp.site %}<div>{{ emp.site }}</div>{% endif %}</div></div>
<div class="tdoc">{{ 'ORDEM DE SERVIÇO' if o.status in ('aprovado', 'producao', 'concluido') else 'ORÇAMENTO' }} Nº {{ '%04d'|format(o.id) }}</div>
<p><b>Cliente:</b> <a href="{{ url_for('cliente_ver', cid=o.cliente_id) }}">{{ o.cliente }}</a>{% if o.telefone %} · {{ o.telefone }}{% endif %}{% if o.email %} · {{ o.email }}{% endif %}
{% if o.endereco or o.cidade %}<br>{{ o.endereco or '' }}{% if o.endereco and o.cidade %} — {% endif %}{{ o.cidade or '' }}{% endif %}<br>
<b>Data do pedido:</b> {{ o.data|dia }}{% if o.entrega %} · <b>Entrega prevista:</b> {{ o.entrega|dia }}{% endif %}{% if o.prazo %} · <b>Prazo:</b> {{ o.prazo }}{% endif %}{% if o.vendedor %}<br><b>Vendedor responsável:</b> {{ o.vendedor }}{% if emp.cargo and o.vendedor == emp.vendedor %} ({{ emp.cargo }}){% endif %}{% endif %}</p>
{% if o.titulo %}<p><b>Referência:</b> {{ o.titulo }}</p>{% endif %}
<table><tr><th>Item</th><th>Produto / serviço</th><th>Medidas</th><th class="n">Qtd.</th><th class="n">Valor unit.</th><th class="n">Total</th></tr>
{% for l in linhas %}<tr><td>{{ loop.index }}</td>
<td><b>{{ l.nome }}</b>{% if l.descricao %}<div class="mut">{{ l.descricao }}</div>{% endif %}
<div class="mut" style="font-size:12px">{% for c in comps[l.id] %}{{ c.categoria or c.descricao }}{% if c.categoria and c.descricao and c.descricao != c.categoria %} ({{ c.descricao }}){% endif %}{% if not loop.last %} · {% endif %}{% endfor %}</div>
<details class="noprint" style="margin-top:4px"><summary class="mut" style="cursor:pointer;font-size:12px">Ver composição de preços</summary>
<table style="margin-top:4px;font-size:13px"><tr><th>Componente</th><th class="n">Por peça</th><th class="n">Preço aplicado</th><th class="n">Subtotal por peça</th></tr>
{% for c in comps[l.id] %}<tr><td>{{ c.categoria or c.descricao }}{% if c.categoria and c.descricao != c.categoria %}<div class="mut">{{ c.descricao }}</div>{% endif %}</td>
<td class="n">{{ c.qtd_unit|qtd(c.unidade) }} {{ c.unidade|ul }}</td><td class="n">{{ c.preco_unit|brl }}{% if c.unidade != 'un' %} / {{ c.unidade|ul }}{% endif %}</td><td class="n">{{ c.sub_unit|brl }}</td></tr>{% endfor %}</table></details></td>
<td class="mut">{% if l.largura and l.altura %}{{ l.largura|num }} × {{ l.altura|num }} cm{% endif %}</td>
<td class="n">{{ l.qtd }}</td><td class="n">{{ l.valor_unit|brl }}</td><td class="n">{{ l.total|brl }}</td></tr>{% endfor %}</table>
<div class="total">{% if o.desconto %}<span class="mut" style="font-size:15px">Subtotal {{ subtotal|brl }} · Desconto{% if o.desconto_tipo == 'pct' %} ({{ o.desconto_valor|num(2) }}%){% endif %} − {{ o.desconto|brl }}</span><br>{% endif %}Total: <b>{{ o.total|brl }}</b></div>
{% if o.obs %}<p><b>Observações:</b> {{ o.obs }}</p>{% endif %}
{% if emp.obs_inst or emp.rodape %}<div class="rodape">{% if emp.obs_inst %}<div>{{ emp.obs_inst }}</div>{% endif %}{% if emp.rodape %}<div>{{ emp.rodape }}</div>{% endif %}</div>{% endif %}
</div>

<div class="card noprint"><h2>Situação do orçamento</h2>
<p class="mut">Ao <b>aprovar</b>, os produtos usados são descontados do estoque. Se o orçamento for <b>cancelado</b>, voltar para <b>em aberto</b>, editado ou excluído, o estoque é ajustado automaticamente.</p><div class="bar">
{% for k, v in ORC.items() %}{% if k != o.status %}<form class="inline" method="post" action="{{ url_for('orcamento_status', oid=o.id) }}"><input type="hidden" name="status" value="{{ k }}"><button class="btn {{ 'red' if k == 'recusado' else ('sec' if k == 'aberto' else '') }} sm">Marcar como: {{ v[0] }}</button></form>{% endif %}{% endfor %}
<form class="inline" method="post" action="{{ url_for('orcamento_excluir', oid=o.id) }}" onsubmit="return confirm('Excluir este orçamento definitivamente?')"><button class="btn red sm">Excluir orçamento</button></form>
</div></div>
{% endblock %}"""

EMPRESA = r"""{% extends "base.html" %}{% block head %}<style>
.sec{margin:0 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.g2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 14px}
.logo-box{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.logo-prev{width:200px;height:110px;border:1px dashed var(--bd);border-radius:10px;display:flex;align-items:center;justify-content:center;background:#fafbfc;overflow:hidden}
.logo-prev img{max-width:100%;max-height:100%}
</style>{% endblock %}{% block conteudo %}
<h1>Empresa</h1><p class="mut">Estes dados saem automaticamente no cabeçalho do orçamento / ordem de serviço impresso. Todos os campos são opcionais.</p>
<form method="post" enctype="multipart/form-data" class="card" id="fEmp" novalidate>
<h2 class="sec">Identificação</h2>
<div class="g2"><div><label>Razão social</label><input name="razao" value="{{ e.razao }}" maxlength="150"></div>
<div><label>Nome fantasia</label><input name="fantasia" value="{{ e.fantasia }}" maxlength="150"></div>
<div><label>CNPJ</label><input name="cnpj" id="cnpj" value="{{ e.cnpj }}" inputmode="numeric" placeholder="00.000.000/0000-00"></div>
<div><label>Inscrição estadual (opcional)</label><input name="ie" value="{{ e.ie }}"></div>
<div><label>Inscrição municipal (opcional)</label><input name="im" value="{{ e.im }}"></div></div>
<h2 class="sec" style="margin-top:18px">Contato</h2>
<div class="g2"><div><label>Telefone principal</label><input name="telefone" class="tel" value="{{ e.telefone }}" inputmode="tel" placeholder="(00) 0000-0000"></div>
<div><label>WhatsApp</label><input name="whatsapp" class="tel" value="{{ e.whatsapp }}" inputmode="tel" placeholder="(00) 00000-0000"></div>
<div><label>E-mail</label><input name="email" type="email" value="{{ e.email }}"></div>
<div><label>Site</label><input name="site" value="{{ e.site }}" placeholder="www.exemplo.com.br"></div></div>
<h2 class="sec" style="margin-top:18px">Endereço</h2>
<div class="g2"><div><label>CEP</label><input name="cep" id="cep" value="{{ e.cep }}" inputmode="numeric" placeholder="00000-000"></div>
<div style="grid-column:span 2"><label>Logradouro</label><input name="logradouro" value="{{ e.logradouro }}"></div>
<div><label>Número</label><input name="numero" value="{{ e.numero }}"></div>
<div><label>Complemento</label><input name="complemento" value="{{ e.complemento }}"></div>
<div><label>Bairro</label><input name="bairro" value="{{ e.bairro }}"></div>
<div><label>Cidade</label><input name="cidade" value="{{ e.cidade }}"></div>
<div><label>Estado</label><select name="estado"><option value=""></option>{% for u in ufs %}<option{{ ' selected' if u == e.estado else '' }}>{{ u }}</option>{% endfor %}</select></div></div>
<h2 class="sec" style="margin-top:18px">Informações comerciais</h2>
<div class="g2"><div><label>Vendedor / responsável padrão</label><input name="vendedor" value="{{ e.vendedor }}" maxlength="80"><div class="hint">Aparece já preenchido em novos orçamentos; você pode trocar em cada um. Mudar aqui não altera os antigos.</div></div>
<div><label>Cargo ou função</label><input name="cargo" value="{{ e.cargo }}"></div></div>
<label>Observações institucionais</label><textarea name="obs_inst" rows="2" maxlength="500">{{ e.obs_inst }}</textarea>
<label>Texto do rodapé dos documentos (opcional)</label><textarea name="rodape" rows="2" maxlength="500">{{ e.rodape }}</textarea>
<h2 class="sec" style="margin-top:18px">Identidade visual</h2>
<div class="logo-box"><div class="logo-prev" id="prev">{% if e.tem_logo %}<img src="{{ url_for('empresa_logo') }}?v={{ e.versao }}" alt="Logotipo">{% else %}<span class="mut">sem logotipo</span>{% endif %}</div>
<div><input type="file" name="logo" id="logo" accept="image/png,image/jpeg"><div class="hint">PNG ou JPG, até 1 MB. Enviar outro arquivo substitui o atual.</div>
{% if e.tem_logo %}<label style="display:flex;gap:6px;align-items:center;margin-top:8px"><input type="checkbox" name="remover_logo" value="1"> Remover logotipo atual</label>{% endif %}</div></div>
<div class="bar" style="margin-top:18px"><button class="btn">Salvar dados da empresa</button></div></form>
<script>
const dg = t => t.replace(/\D/g, '');
function mCnpj(v) { v = dg(v).slice(0, 14); return v.replace(/^(\d{2})(\d)/, '$1.$2').replace(/^(\d{2})\.(\d{3})(\d)/, '$1.$2.$3').replace(/\.(\d{3})(\d)/, '.$1/$2').replace(/(\d{4})(\d)/, '$1-$2'); }
function mCep(v) { v = dg(v).slice(0, 8); return v.replace(/^(\d{5})(\d)/, '$1-$2'); }
function mTel(v) { v = dg(v).slice(0, 11); if (v.length > 10) return v.replace(/^(\d{2})(\d{5})(\d)/, '($1) $2-$3'); if (v.length > 6) return v.replace(/^(\d{2})(\d{4})(\d)/, '($1) $2-$3'); return v.replace(/^(\d{2})(\d)/, '($1) $2'); }
document.getElementById('cnpj').addEventListener('input', e => e.target.value = mCnpj(e.target.value));
document.getElementById('cep').addEventListener('input', e => e.target.value = mCep(e.target.value));
document.querySelectorAll('.tel').forEach(i => i.addEventListener('input', e => e.target.value = mTel(e.target.value)));
document.getElementById('logo').addEventListener('change', e => { const f = e.target.files[0]; if (!f) return;
  if (!/^image\/(png|jpeg)$/.test(f.type) || f.size > 1048576) { alert('Envie uma imagem PNG ou JPG de até 1 MB.'); e.target.value = ''; return; }
  const im = document.createElement('img'); im.src = URL.createObjectURL(f); document.getElementById('prev').replaceChildren(im); });
</script>
{% endblock %}"""

CATEGORIAS = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><h1>Categorias de produtos</h1><div class="bar"><a class="btn sec" href="{{ url_for('duplicidades') }}">Duplicidades de SKU/telefone</a><a class="btn sec" href="{{ url_for('produtos') }}">Voltar aos produtos</a></div></div>
<div class="card"><form method="post" action="{{ url_for('categoria_nova') }}" class="bar" style="align-items:end"><div style="flex:1;min-width:220px"><label>Nova categoria</label><input name="nome" maxlength="60" placeholder="Ex.: Ferragens"></div><button class="btn">Cadastrar</button></form></div>
<div class="card"><table><tr><th>Nome</th><th>Produtos</th><th>Situação</th><th></th></tr>
{% for c in rows %}<tr><td><form method="post" action="{{ url_for('categoria_renomear', cid=c.id) }}" class="bar" style="margin:0;flex-wrap:nowrap"><input name="nome" value="{{ c.nome }}" maxlength="60" style="min-width:160px"><button class="btn sec sm">Renomear</button></form></td>
<td>{{ c.usos }}</td><td><span class="tag {{ 'ok' if c.ativo else '' }}">{{ 'ativa' if c.ativo else 'inativa' }}</span></td>
<td class="n"><form class="inline" method="post" action="{{ url_for('categoria_ativar', cid=c.id) }}"><button class="btn sec sm">{{ 'Desativar' if c.ativo else 'Ativar' }}</button></form>
{% if c.usos %}<form class="inline" method="post" action="{{ url_for('categoria_reclassificar', cid=c.id) }}" onsubmit="return confirm('Mover todos os produtos desta categoria para a categoria escolhida?')"><select name="destino" style="width:auto"><option value="">mover produtos para…</option>{% for d in rows if d.id != c.id and d.ativo %}<option value="{{ d.id }}">{{ d.nome }}</option>{% endfor %}</select> <button class="btn sec sm">Reclassificar</button></form>
{% else %}<form class="inline" method="post" action="{{ url_for('categoria_excluir', cid=c.id) }}" onsubmit="return confirm('Excluir esta categoria?')"><button class="btn red sm">Excluir</button></form>{% endif %}</td></tr>{% endfor %}</table>
<p class="hint">Renomear atualiza os produtos da categoria. Categoria em uso não pode ser excluída (reclassifique antes). Desativar só a tira da lista de novos cadastros.</p></div>
{% endblock %}"""

DUPLICIDADES = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Duplicidades de SKU e telefone</h1>
<div class="card"><p>SKU e telefone principal devem ser únicos. Nada é apagado ou mesclado automaticamente: corrija os cadastros abaixo (edite um dos dois) e reinicie o sistema para a trava do banco de dados ser ativada.</p>
<p>Trava de SKU no banco: <span class="tag {{ 'ok' if idx_sku else 'wa' }}">{{ 'ativa' if idx_sku else 'aguardando correção' }}</span> · Trava de telefone no banco: <span class="tag {{ 'ok' if idx_tel else 'wa' }}">{{ 'ativa' if idx_tel else 'aguardando correção' }}</span></p></div>
<div class="card"><h2>SKU repetido</h2>{% if skus %}{% for k, ps in skus %}<p><b>{{ k }}</b></p><table>{% for p in ps %}<tr><td>#{{ p.id }}</td><td>{{ p.nome }}{% if not p.ativo %} <span class="tag">arquivado</span>{% endif %}</td><td>{{ p.sku }}</td><td class="n"><a class="btn sec sm" href="{{ url_for('produto_form', pid=p.id) }}">Editar</a></td></tr>{% endfor %}</table>{% endfor %}{% else %}<p class="mut">Nenhum SKU repetido.</p>{% endif %}</div>
<div class="card"><h2>Telefone repetido</h2>{% if tels %}{% for k, cs in tels %}<p><b>{{ k }}</b></p><table>{% for c in cs %}<tr><td>#{{ c.id }}</td><td>{{ c.nome }}</td><td>{{ c.telefone }}</td><td class="n"><a class="btn sec sm" href="{{ url_for('cliente_form', cid=c.id) }}">Editar</a></td></tr>{% endfor %}</table>{% endfor %}{% else %}<p class="mut">Nenhum telefone repetido.</p>{% endif %}</div>
{% endblock %}"""

IMPRIMIR = r"""{% macro tabela(linhas, comps, interna) %}
<table class="it"><thead><tr><th class="q">Qtd.</th><th>Descrição</th><th class="m">Medidas</th><th class="v">Valor unit.</th><th class="v">Total</th></tr></thead><tbody>
{% for l in linhas %}<tr><td class="q">{{ l.qtd }}</td><td><b>{{ l.nome }}</b>{% if l.descricao %} — {{ l.descricao }}{% endif %}{% if comps[l.id] %}<div class="sm">{% for c in comps[l.id] %}{{ c.categoria or c.descricao }}{% if c.categoria and c.descricao and c.descricao != c.categoria %} ({{ c.descricao }}){% endif %}{% if not loop.last %} · {% endif %}{% endfor %}</div>{% endif %}</td>
<td class="m">{% if l.largura and l.altura %}{{ l.largura|num }} × {{ l.altura|num }} cm{% endif %}</td><td class="v">{{ l.valor_unit|brl }}</td><td class="v">{{ l.total|brl }}</td></tr>{% endfor %}</tbody></table>
<div class="tot">{% if o.desconto %}<div>Subtotal: {{ subtotal|brl }}</div><div>Desconto{% if o.desconto_tipo == 'pct' %} ({{ o.desconto_valor|num(2) }}%){% endif %}: − {{ o.desconto|brl }}</div>{% endif %}<div class="gt">Total: {{ o.total|brl }}</div></div>
{% if o.obs %}<div class="obs"><b>Observações:</b> {{ o.obs }}</div>{% endif %}
{% endmacro %}
{% macro dados(o, interna) %}
<div class="ids"><span><b>Nº</b> {{ '%04d'|format(o.id) }}</span><span><b>Data do pedido:</b> {{ o.data|dia }}</span>{% if o.entrega %}<span><b>Entrega prevista:</b> {{ o.entrega|dia }}</span>{% endif %}{% if o.vendedor %}<span><b>Vendedor:</b> {{ o.vendedor }}</span>{% endif %}</div>
<div class="cli"><b>Cliente:</b> {{ o.cliente }}{% if interna and o.telefone %} · Tel.: {{ o.telefone }}{% endif %}{% if not interna and o.telefone %} · {{ o.telefone }}{% endif %}{% if not interna and (o.endereco or o.cidade) %}<br>{{ o.endereco or '' }}{% if o.endereco and o.cidade %} — {% endif %}{{ o.cidade or '' }}{% endif %}{% if o.titulo %}<br><b>Referência:</b> {{ o.titulo }}{% endif %}{% if o.prazo %}<br><b>Prazo:</b> {{ o.prazo }}{% endif %}</div>
{% endmacro %}
{% macro cabecalho(emp) %}
<div class="cab"><div class="lg">{% if emp.tem_logo %}<img src="{{ url_for('empresa_logo') }}?print=1&v={{ emp.versao }}" alt="">{% endif %}</div>
<div class="em"><div class="nm">{{ emp.nome }}</div>{% if emp.razao_extra %}<div>{{ emp.razao_extra }}</div>{% endif %}{% if emp.cnpj %}<div>CNPJ: {{ emp.cnpj }}</div>{% endif %}{% if emp.linha_rua %}<div>{{ emp.linha_rua }}</div>{% endif %}{% if emp.linha_cidade %}<div>{{ emp.linha_cidade }}</div>{% endif %}{% if emp.fones %}<div>{{ emp.fones }}</div>{% endif %}{% if emp.email %}<div>{{ emp.email }}</div>{% endif %}{% if emp.site %}<div>{{ emp.site }}</div>{% endif %}</div></div>
{% endmacro %}<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ 'Ordem de serviço' if modo == 'os' else 'Orçamento' }} {{ '%04d'|format(o.id) }}</title>
<style>
@page{size:A4 portrait;margin:8mm}
*{box-sizing:border-box}
body{margin:0;font:10pt/1.3 Arial,Helvetica,sans-serif;color:#000;background:#e5e7eb}
.bar{max-width:210mm;margin:10px auto;display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:14px}
.bar button,.bar a{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;text-decoration:none;cursor:pointer}.bar a.s{background:#6b7280}
.folha{width:210mm;margin:0 auto 20px;background:#fff;padding:8mm;box-shadow:0 1px 6px #0003}
.via{padding:1mm 2mm}
.duas.cabe .via{min-height:134mm}
.cab{display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1.5px solid #000;padding-bottom:3px;margin-bottom:4px}
.lg{flex:0 0 38mm;max-width:38mm}.lg img{max-width:100%;max-height:16mm;display:block}
.em{text-align:right;font-size:8.5pt;line-height:1.25}.em .nm{font-size:12pt;font-weight:700}
.tit{font-size:11.5pt;font-weight:700;letter-spacing:.02em;margin:3px 0}
.ids{display:flex;flex-wrap:wrap;gap:2px 14px;font-size:9.5pt}.cli{font-size:9.5pt;margin:3px 0 4px}
table.it{width:100%;border-collapse:collapse;font-size:9.5pt}
.it th{border-bottom:1px solid #000;text-align:left;padding:2px 3px;font-size:8.5pt}
.it td{border-bottom:.5px solid #999;padding:2px 3px;vertical-align:top}
.it .q{width:11mm;text-align:center}.it .m{width:26mm;white-space:nowrap}.it .v{width:24mm;text-align:right;white-space:nowrap}
.sm{font-size:8pt;color:#333}
.tot{text-align:right;margin-top:4px;font-size:9.5pt}.tot .gt{font-size:12pt;font-weight:700;border-top:1px solid #000;display:inline-block;padding-top:2px;margin-top:2px}
.obs{font-size:9pt;margin-top:4px}
.rod{font-size:8pt;color:#333;border-top:.5px solid #999;margin-top:6px;padding-top:2px}
.corte{border-top:1.5px dashed #000;margin:2mm 0;position:relative;height:0}.corte span{position:absolute;left:50%;top:-8px;transform:translateX(-50%);background:#fff;padding:0 6px;font-size:7.5pt}
.duas:not(.cabe) .via2{break-before:page}.duas:not(.cabe) .corte{display:none}
.aviso{max-width:210mm;margin:0 auto 8px;font-size:13px;background:#fef3c7;padding:6px 10px;border-radius:6px}
@media print{body{background:#fff}.bar,.aviso{display:none!important}.folha{width:auto;margin:0;padding:0;box-shadow:none}}
</style></head><body>
<div class="bar"><button type="button" onclick="window.print()">Imprimir</button><a class="s" href="{{ url_for('orcamento_ver', oid=o.id) }}">Voltar ao orçamento</a></div>
{% if modo == '2vias' and not cabe %}<div class="aviso">Orçamento longo: para não cortar nem reduzir demais a letra, cada via sai em uma folha A4.</div>{% endif %}
<div class="folha {{ 'duas' if modo == '2vias' else '' }} {{ 'cabe' if cabe else '' }}">
{% if modo == '2vias' %}
<section class="via via1">{{ cabecalho(emp) }}<div class="tit">ORÇAMENTO — VIA DO CLIENTE</div>{{ dados(o, false) }}{{ tabela(linhas, comps, false) }}{% if emp.obs_inst or emp.rodape %}<div class="rod">{{ emp.obs_inst }}{% if emp.obs_inst and emp.rodape %} · {% endif %}{{ emp.rodape }}</div>{% endif %}</section>
<div class="corte"><span>✂ corte aqui</span></div>
<section class="via via2"><div class="tit" style="border-bottom:1.5px solid #000;padding-bottom:3px">ORÇAMENTO — VIA INTERNA</div>{{ dados(o, true) }}{{ tabela(linhas, comps, true) }}</section>
{% else %}
<section class="via">{{ cabecalho(emp) }}<div class="tit">{{ 'ORDEM DE SERVIÇO' if modo == 'os' else 'ORÇAMENTO' }} Nº {{ '%04d'|format(o.id) }}</div>{{ dados(o, false) }}{{ tabela(linhas, comps, false) }}{% if emp.obs_inst or emp.rodape %}<div class="rod">{{ emp.obs_inst }}{% if emp.obs_inst and emp.rodape %} · {% endif %}{{ emp.rodape }}</div>{% endif %}</section>
{% endif %}
</div>
{% if auto %}<script>window.addEventListener('load',function(){window.print();});</script>{% endif %}
</body></html>"""

EXTRATO_ENVIO = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><h1>Importar extrato bancário</h1><div class="bar"><a class="btn sec" href="{{ url_for('bancos') }}">Voltar a Bancos</a></div></div>
<div class="card"><p>Envie o PDF do extrato do <b>Sicoob</b> (conta corrente), do <b>Mercado Pago</b> ou do <b>Nubank</b>. As movimentações ficam na página <b>Bancos</b>, separadas do Financeiro e das vendas, por isso <b>não entram na DRE nem duplicam receitas</b>. Nada é gravado antes da sua conferência: você vê cada linha, ajusta se quiser e só então confirma.</p>
<form method="post" action="{{ url_for('extrato_enviar') }}" enctype="multipart/form-data" class="bar" style="align-items:end"><div><label>Arquivo PDF (até 8 MB)</label><input type="file" name="arquivo" accept="application/pdf,.pdf" required></div><button class="btn">Ler extrato</button></form>
<p class="hint">Enviar o mesmo extrato (ou outro que repita dias já importados) não duplica lançamentos.</p></div>
<div class="card"><h2>Extratos enviados</h2>{% if rows %}<table><tr><th>Banco / conta</th><th>Período</th><th>Conferência do saldo</th><th class="n">Linhas lançadas</th><th></th></tr>
{% for e in rows %}<tr><td>{{ e.banco }} {{ e.conta }}</td><td>{{ e.ini|dia }} a {{ e.fim|dia }}</td><td><span class="tag {{ 'ok' if e.confere else 'er' }}">{{ 'confere' if e.confere else 'não confere' }}</span></td><td class="n">{{ e.ok }} de {{ e.total }}</td><td class="n"><a class="btn sm" href="{{ url_for('extrato_ver', eid=e.id) }}">Abrir</a></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum extrato enviado ainda.</p>{% endif %}</div>
{% endblock %}"""

EXTRATO_VER = r"""{% extends "base.html" %}{% block head %}<style>
.ln td{vertical-align:top}.ln select{width:100%;min-width:130px}.det{font-size:12px;color:var(--mut)}
.c{color:#15803d;white-space:nowrap}.d{color:#b91c1c;white-space:nowrap}
tr.feito td{background:#f0fdf4}tr.cinza td{color:var(--mut);background:#f8fafc}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><h1>Conferir extrato {{ e.banco }}</h1><div class="bar"><a class="btn sec" href="{{ url_for('extrato_envio') }}">Voltar</a></div></div>
<div class="card"><p><b>{{ e.titular }}</b> · conta {{ e.conta }} · {{ e.ini|dia }} a {{ e.fim|dia }}</p>
<div class="row4"><div><small class="mut">Saldo anterior</small><br><b>{{ e.saldo_ant|brl }}</b></div><div><small class="mut">Entradas</small><br><b class="c">{{ ent|brl }}</b></div><div><small class="mut">Saídas</small><br><b class="d">{{ sai|brl }}</b></div><div><small class="mut">Saldo final</small><br><b>{{ e.saldo_final|brl }}</b></div></div>
{% if e.confere %}<p style="margin:10px 0 0"><span class="tag ok">O saldo do extrato fecha com as linhas lidas</span></p>
{% else %}<div class="flash erro" style="margin-top:10px">As linhas lidas NÃO fecham com o saldo do extrato. Pode ter faltado ou sobrado uma linha na leitura: confira com o PDF antes de importar.</div>{% endif %}</div>
<form method="post" action="{{ url_for('extrato_confirmar', eid=e.id) }}"><div class="card"><table class="ln"><tr><th>Data</th><th>Descrição</th><th class="n">Valor</th><th style="width:170px">O que fazer</th><th style="width:210px">Categoria</th></tr>
{% for l in lin %}{% set det = l.detalhe.split('\n') if l.detalhe else [] %}
<tr class="{{ 'feito' if l.aplicado else ('cinza' if l.acao in ('duplicada','retido') else '') }}"><td>{{ l.data|dia }}</td>
<td><b>{{ l.historico }}</b>{% set nome = contraparte(det) %}{% if nome %} — {{ nome }}{% endif %}<div class="det">{{ det[:3]|join(' · ') }}</div>{% if l.nota %}<div class="det"><i>{{ l.nota }}</i></div>{% endif %}</td>
<td class="n {{ 'c' if l.sinal == 'C' else 'd' }}">{{ '+' if l.sinal == 'C' else ('−' if l.sinal == 'D' else '') }}{{ l.valor|brl }}</td>
{% if l.aplicado %}<td colspan="2"><span class="tag ok">em Bancos: {{ {'receita':'entrada','despesa':'saída','ignorar':'ignorado'}[l.acao] }}</span></td>
{% elif l.acao in ('duplicada','retido') %}<td colspan="2"><span class="tag">{{ 'já importada' if l.acao == 'duplicada' else 'retido' }}</span></td>
{% else %}<td><select name="acao_{{ l.id }}" data-l="{{ l.id }}" data-s="{{ l.sinal }}"><option value="decidir"{{ ' selected' if l.acao == 'decidir' else '' }}>Decidir depois</option>
{% if l.sinal == 'C' %}<option value="receita"{{ ' selected' if l.acao == 'receita' else '' }}>Entrada ({{ e.banco }})</option>
{% else %}<option value="despesa"{{ ' selected' if l.acao == 'despesa' else '' }}>Saída ({{ e.banco }})</option>{% endif %}<option value="ignorar"{{ ' selected' if l.acao == 'ignorar' else '' }}>Ignorar</option></select></td>
<td><select name="cat_{{ l.id }}" id="cat_{{ l.id }}"><option value="">—</option>{% for c in cats if c.tipo == ('receita' if l.sinal == 'C' else 'despesa') %}<option value="{{ c.id }}"{{ ' selected' if c.id == l.categoria_id else '' }}>{{ c.nome }}</option>{% endfor %}</select>
</td>{% endif %}</tr>{% endfor %}</table></div>
<div class="bar"><button class="btn">Confirmar importação</button><span class="mut">Já vem tudo sugerido como entrada/saída do banco; só "Ignorar" ou "Decidir depois" deixam a linha de fora.{% if pend %} Pendentes: {{ pend }}.{% endif %}</span></div></form>
<div class="bar" style="margin-top:14px"><form class="inline" method="post" action="{{ url_for('extrato_desfazer', eid=e.id) }}" onsubmit="return confirm('Remover os lançamentos criados por este extrato?')"><button class="btn sec sm">Desfazer importação</button></form>
<form class="inline" method="post" action="{{ url_for('extrato_excluir', eid=e.id) }}" onsubmit="return confirm('Remover este extrato?')"><button class="btn red sm">Excluir extrato</button></form></div>
<script>
document.querySelectorAll('select[data-l]').forEach(a => { const id = a.dataset.l, c = document.getElementById('cat_' + id), v = document.getElementById('venda_' + id);
  const upd = () => { c.style.display = (a.value === 'receita' || a.value === 'despesa') ? '' : 'none'; if (v) v.style.display = a.value === 'vincular' ? '' : 'none'; };
  a.addEventListener('change', upd); upd(); });
</script>
{% endblock %}"""

BANCOS_CAT = r"""{% extends "base.html" %}{% block head %}<style>
.nv{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.nv input,.nv select{width:auto}
td form{display:inline-flex;gap:6px;align-items:center;margin:0}td input{width:190px}
.c{color:#15803d}.d{color:#b91c1c}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Categorias dos bancos</h1><span class="mut">Servem só para a página Bancos; não mexem no Financeiro nem na DRE.</span></div>
<a class="btn sec" href="{{ url_for('bancos') }}">Voltar a Bancos</a></div>
<div class="card"><h2>Nova categoria</h2><form method="post" action="{{ url_for('bancos_categoria_nova') }}" class="nv">
<div><label>Nome</label><input name="nome" maxlength="60" required></div>
<div><label>Tipo</label><select name="tipo"><option value="despesa">Saída (despesa)</option><option value="receita">Entrada (receita)</option></select></div><button class="btn">Adicionar</button></form></div>
{% for tipo, titulo in (('receita','Entradas'),('despesa','Saídas')) %}
<div class="card"><h2>{{ titulo }}</h2><table><tr><th>Categoria</th><th class="n">Movimentos</th><th></th></tr>
{% for c in cats if c.tipo == tipo %}<tr><td><form method="post" action="{{ url_for('bancos_categoria_renomear', cid=c.id) }}"><input name="nome" value="{{ c.nome }}" maxlength="60"><button class="btn sec sm">Renomear</button></form></td>
<td class="n">{{ uso.get(c.id, 0) }}</td>
<td class="n"><form method="post" action="{{ url_for('bancos_categoria_excluir', cid=c.id) }}" data-n="{{ uso.get(c.id, 0) }}" onsubmit="return confirm(this.dataset.n > 0 ? 'Excluir a categoria? Os ' + this.dataset.n + ' movimentos dela irão para a categoria escolhida (ou ficarão sem categoria).' : 'Excluir esta categoria?')">
{% if uso.get(c.id, 0) %}<select name="destino"><option value="">Mover para: sem categoria</option>{% for o in cats if o.tipo == tipo and o.id != c.id %}<option value="{{ o.id }}">Mover para: {{ o.nome }}</option>{% endfor %}</select>{% endif %}
<button class="btn red sm">Excluir</button></form></td></tr>
{% else %}<tr><td colspan="3" class="mut">Nenhuma categoria.</td></tr>{% endfor %}</table></div>{% endfor %}
{% endblock %}"""

AVALIACOES = r"""{% extends "base.html" %}{% block head %}<style>
.ab{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.ab a{padding:5px 12px;border-radius:999px;border:1px solid var(--bd);background:#fff;color:inherit;text-decoration:none;font-size:13.5px}
.ab a.on{background:#eff6ff;border-color:#bfdbfe;color:var(--pri);font-weight:600}
.acoes{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}.acoes form{margin:0}.acoes .btn{white-space:nowrap}
.wpp{background:#16a34a;color:#fff}.wpp:hover{background:#15803d}
textarea.msg{width:100%;min-height:150px;font:inherit}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Avaliações no Google</h1><span class="mut">Quando o serviço é concluído, envie ao cliente pelo WhatsApp um agradecimento com o link para avaliar a loja.</span></div></div>
{% if not link %}<div class="alerta">Cadastre abaixo o link de avaliação do Google para liberar o envio.</div>{% endif %}
<div class="ab">{% for k, r in (('pendentes','Pendentes'),('enviadas','Enviadas'),('dispensadas','Não enviar'),('todas','Todas')) %}<a class="{{ 'on' if aba == k else '' }}" href="{{ url_for('avaliacoes', aba=k) }}">{{ r }} ({{ cont[k] }})</a>{% endfor %}</div>
<div class="card">{% if rows %}<table><tr><th>Serviço</th><th>Cliente</th><th>Concluído</th><th>Situação</th><th></th></tr>
{% for o in rows %}{% set num = numero_whatsapp(o.telefone) %}<tr>
<td><a href="{{ url_for('orcamento_ver', oid=o.id) }}">#{{ o.id }}</a>{% if o.titulo %} <span class="mut">{{ o.titulo }}</span>{% endif %}</td>
<td>{{ o.cliente }}<br><span class="mut" style="font-size:12.5px">{{ o.telefone or 'sem telefone' }}</span></td>
<td>{{ (o.concluido_em or '')|dt }}</td>
<td>{% if o.aval_enviada_em %}<span class="tag ok">enviada {{ o.aval_enviada_em|dt }}</span>{% elif o.aval_dispensada %}<span class="tag">não enviar</span>{% else %}<span class="tag wa">pendente</span>{% endif %}</td>
<td><div class="acoes">
{% if num and link %}<form method="post" action="{{ url_for('avaliacao_enviar', oid=o.id) }}" target="_blank"><button class="btn sm wpp">{{ '💬 Reenviar' if o.aval_enviada_em else '💬 Enviar no WhatsApp' }}</button></form>
{% else %}<span class="mut" style="font-size:12.5px">{{ 'sem telefone válido' if not num else 'falta o link' }}</span>{% endif %}
{% if not o.aval_enviada_em and not o.aval_dispensada %}<form method="post" action="{{ url_for('avaliacao_estado', oid=o.id) }}"><input type="hidden" name="acao" value="enviada"><button class="btn sec sm" title="Já enviei por fora">Marcar enviada</button></form>
<form method="post" action="{{ url_for('avaliacao_estado', oid=o.id) }}"><input type="hidden" name="acao" value="dispensar"><button class="btn ghost sm">Não enviar</button></form>
{% else %}<form method="post" action="{{ url_for('avaliacao_estado', oid=o.id) }}"><input type="hidden" name="acao" value="pendente"><button class="btn ghost sm">Voltar p/ pendente</button></form>{% endif %}
</div></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum serviço nesta lista. Os serviços aparecem aqui quando o orçamento é marcado como <b>Concluído / entregue</b>.</p>{% endif %}</div>
<div class="card"><h2>Configuração</h2>
<form method="post" action="{{ url_for('avaliacoes_config') }}">
<label>Link de avaliação do Google</label><input name="link" value="{{ link }}" placeholder="https://g.page/r/.../review" maxlength="300">
<p class="nota">Para pegar o link: Google Meu Negócio / Perfil da Empresa → “Pedir avaliações” → copiar o link.</p>
<label>Mensagem</label><textarea class="msg" name="msg" maxlength="800">{{ modelo }}</textarea>
<p class="nota">Use <b>{nome}</b> (primeiro nome do cliente), <b>{empresa}</b>, <b>{os}</b> (número do serviço) e <b>{link}</b>. O link é sempre incluído.</p>
<button class="btn">Salvar</button></form></div>
{% endblock %}
"""

BANCOS = r"""{% extends "base.html" %}{% block head %}<style>
.c{color:#15803d;white-space:nowrap}.d{color:#b91c1c;white-space:nowrap}
.kp{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}.kp .card{margin:0;padding:14px 16px}.kp small{color:var(--mut);display:block}.kp b{font-size:22px;font-variant-numeric:tabular-nums}
.qtd{display:inline-block;min-width:22px;text-align:center;padding:0 7px;border-radius:999px;background:#eff6ff;color:var(--pri);font-size:12.5px;text-decoration:none;border:1px solid #bfdbfe}.qtd:hover{background:#dbeafe}
.chip{display:inline-flex;gap:8px;align-items:center;background:#eff6ff;border:1px solid #bfdbfe;border-radius:999px;padding:3px 10px;font-size:13px;margin-left:8px}
.sf{display:flex;gap:6px;align-items:center}.sf input{width:130px;text-align:right}td .sf{margin:0}
.fl{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.fl select,.fl input{width:auto}.fl label{margin:0 0 3px}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Bancos</h1><span class="mut">Movimentação dos extratos importados. Fica separada das vendas e do Financeiro, então não duplica receitas nem entra na DRE.</span></div>
<div class="bar"><a class="btn sec" href="{{ url_for('bancos_categorias') }}">Categorias</a><a class="btn" href="{{ url_for('extrato_envio') }}">Importar extrato</a></div></div>
<form method="get" class="fl" style="margin-bottom:14px"><div><label>Mês</label><select name="mes"><option value="0">Ano todo</option>{% for m in meses %}<option value="{{ loop.index }}"{{ ' selected' if loop.index == mes else '' }}>{{ m }}</option>{% endfor %}</select></div>
<div><label>Ano</label><input type="number" name="ano" value="{{ ano }}" min="2000" max="2100" style="width:96px"></div>
<div><label>Banco</label><select name="banco"><option value="">Todos</option>{% for b in lista_bancos %}<option{{ ' selected' if b == banco else '' }}>{{ b }}</option>{% endfor %}</select></div><button class="btn sec">Filtrar</button></form>
<div class="kp"><div class="card"><small>Entradas</small><b class="c">{{ tot.ent|brl }}</b></div><div class="card"><small>Saídas</small><b class="d">{{ tot.sai|brl }}</b></div>
<div class="card"><small>Saldo do período</small><b class="{{ 'c' if tot.ent >= tot.sai else 'd' }}">{{ (tot.ent - tot.sai)|brl }}</b></div><div class="card"><small>Movimentações</small><b>{{ tot.n }}</b></div></div>
{% if por_banco %}<div class="row2"><div class="card"><h2>Por banco</h2><table><tr><th>Banco</th><th class="n">Entradas</th><th class="n">Saídas</th><th class="n">Saldo</th></tr>
{% for b in por_banco %}<tr><td>{{ b.banco }} <span class="mut">({{ b.n }})</span></td><td class="n c">{{ b.ent|brl }}</td><td class="n d">{{ b.sai|brl }}</td><td class="n">{{ (b.ent - b.sai)|brl }}</td></tr>{% endfor %}</table></div>
<div class="card"><h2>Por categoria</h2><table>{% for c in por_cat %}<tr><td>{{ c.cat }} <a class="qtd" href="{{ url_for('bancos', ano=ano, mes=mes, banco=banco, cat=c.cid) }}#movs" title="Ver só os movimentos desta categoria">{{ c.n }}</a></td><td class="n {{ 'c' if c.acao == 'receita' else 'd' }}">{{ '+' if c.acao == 'receita' else '−' }} {{ c.t|brl }}</td></tr>{% endfor %}</table></div></div>{% endif %}
{% if mes %}<div class="card" id="saldos"><h2>Saldo das contas — {{ meses[mes - 1] }}/{{ ano }}</h2>
<p class="nota">No fechamento do mês, digite quanto sobrou em cada conta. Esse valor vira o <b>saldo inicial</b> do mês seguinte. O <b>saldo calculado</b> = inicial + entradas − saídas ± transferências ignoradas; a diferença mostra se falta importar algo.</p>
{% if saldos %}<table><tr><th>Banco</th><th class="n">Saldo inicial</th><th class="n">Entradas</th><th class="n">Saídas</th><th class="n">Transf. ignoradas</th><th class="n">Saldo calculado</th><th>Saldo no fechamento</th><th class="n">Diferença</th></tr>
{% for s in saldos %}<tr><td>{{ s.banco }}</td>
<td class="n">{% if s.inicial is not none %}{{ s.inicial|brl }}<br><span class="mut" style="font-size:11.5px">{{ s.origem }}</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n c">{{ s.ent|brl }}</td><td class="n d">{{ s.sai|brl }}</td><td class="n">{{ s.ign|brl }}</td>
<td class="n">{% if s.calc is not none %}{{ s.calc|brl }}{% else %}<span class="mut">—</span>{% endif %}</td>
<td><form method="post" action="{{ url_for('bancos_saldo') }}" class="sf"><input type="hidden" name="banco" value="{{ s.banco }}"><input type="hidden" name="mes" value="{{ ano }}-{{ '%02d'|format(mes) }}"><input type="hidden" name="filtro_banco" value="{{ banco }}">
<input name="saldo" value="{{ ('%.2f'|format(s.inf / 100)).replace('.', ',') if s.inf is not none else '' }}" placeholder="0,00" inputmode="decimal"><button class="btn sm">Salvar</button></form></td>
<td class="n">{% if s.dif is not none %}<span class="{{ 'c' if s.dif == 0 else 'd' }}">{{ 'confere' if s.dif == 0 else s.dif|brl }}</span>{% else %}<span class="mut">—</span>{% endif %}</td></tr>{% endfor %}</table>
<p class="nota">O saldo informado em {{ meses[mes - 1] }} será o saldo inicial de {{ mes_seg[5:] }}/{{ mes_seg[:4] }}. Para limpar um fechamento, salve o campo em branco.</p>
{% else %}<p class="mut">Importe um extrato para controlar o saldo da conta.</p>{% endif %}</div>{% endif %}
<div class="card" id="movs"><h2>Movimentações{% if cat_nome %}<span class="chip">Categoria: <b>{{ cat_nome }}</b> · {{ (sub_ent or sub_sai)|brl }} <a href="{{ url_for('bancos', ano=ano, mes=mes, banco=banco) }}#movs">limpar ✕</a></span>{% endif %}</h2>{% if movs %}<table><tr><th>Data</th><th>Descrição</th><th>Categoria</th><th>Banco</th><th class="n">Valor</th></tr>
{% for l in movs %}{% set det = l.detalhe.split('\n') if l.detalhe else [] %}<tr><td>{{ l.data|dia }}</td><td>{{ l.historico }}{% set nome = contraparte(det) %}{% if nome %} <span class="mut">— {{ nome }}</span>{% endif %}</td><td>{{ l.cat or '' }}</td><td>{{ l.banco }}</td>
<td class="n {{ 'c' if l.acao == 'receita' else 'd' }}">{{ '+' if l.acao == 'receita' else '−' }} {{ l.valor|brl }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhuma movimentação neste período. Importe um extrato para começar.</p>{% endif %}</div>
<div class="card"><h2>Extratos importados</h2>{% if extratos %}<table><tr><th>Banco / conta</th><th>Período</th><th>Saldo</th><th class="n">Movimentações</th><th class="n">Pendentes</th><th></th></tr>
{% for e in extratos %}<tr><td>{{ e.banco }} {{ e.conta }}</td><td>{{ e.ini|dia }} a {{ e.fim|dia }}</td><td><span class="tag {{ 'ok' if e.confere else 'er' }}">{{ 'confere' if e.confere else 'não confere' }}</span></td><td class="n">{{ e.ok }}</td><td class="n">{{ e.pend }}</td><td class="n"><a class="btn sec sm" href="{{ url_for('extrato_ver', eid=e.id) }}">Abrir</a></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum extrato enviado ainda.</p>{% endif %}</div>
{% endblock %}"""

VENDAS = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Vendas</h1>
<div class="bar"><a class="btn" href="{{ url_for('venda_nova') }}">+ Nova venda</a>
<span class="mut">Orçamentos marcados como <b>Concluído</b> entram aqui automaticamente, uma única vez.</span></div>
<div class="card">{% if rows %}<table><tr><th>#</th><th>Data</th><th>Cliente</th><th>Origem</th><th class="n">Total</th><th>Situação</th><th>Recebimento</th></tr>
{% for x in rows %}<tr><td><a href="{{ url_for('venda_ver', vid=x.id) }}">{{ x.id }}</a></td><td>{{ x.data|dt }}</td><td>{{ x.cliente or 'Consumidor' }}</td>
<td>{% if x.orcamento_id %}<a href="{{ url_for('orcamento_ver', oid=x.orcamento_id) }}">Orçamento #{{ x.orcamento_id }}</a>{% else %}<span class="mut">Venda direta</span>{% endif %}</td>
<td class="n">{{ x.total|brl }}</td>
<td><span class="tag {{ 'er' if x.status == 'cancelada' else 'ok' }}">{{ x.status }}</span></td>
<td>{% if x.status == 'ativa' %}{% if x.recebido_em %}<span class="tag ok">recebido {{ x.recebido_em|dia }}</span>{% else %}<span class="tag wa">a receber</span>{% endif %}{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhuma venda registrada.</p>{% endif %}</div>
{% endblock %}"""

VENDA_NOVA = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Nova venda</h1>
<p class="mut">Para vendas de peças prontas e serviços por unidade. Trabalhos sob medida (vidro, moldura) vão em <a href="{{ url_for('orcamento_form') }}">Orçamentos</a>.</p>
<form class="card" method="post">
<label>Cliente</label>
<select name="cliente_id"><option value="">Consumidor (sem cadastro)</option>{% for c in cl %}<option value="{{ c.id }}">{{ c.nome }}</option>{% endfor %}</select>
<label>Itens</label>
<table id="itens"><thead><tr><th>Produto</th><th style="width:90px">Qtd</th><th style="width:130px">Preço unit.</th><th class="n" style="width:120px">Subtotal</th><th style="width:40px"></th></tr></thead><tbody></tbody></table>
<p><button type="button" class="btn sec sm" onclick="addLinha()">+ Adicionar item</button></p>
<div class="total">Total: <b id="total">R$ 0,00</b></div>
<label>Observações</label><textarea name="obs" rows="2"></textarea>
<p><button class="btn">Registrar venda</button> <a class="btn sec" href="{{ url_for('vendas') }}">Cancelar</a></p>
</form>
<script>
const PRODUTOS = {{ prod|tojson }};
function fmt(v){return 'R$ '+v.toFixed(2).replace('.',',').replace(/\B(?=(\d{3})+(?!\d))/g,'.');}
function num(s){s=(s||'0').toString().replace('R$','').trim(); if(s.indexOf(',')>=0) s=s.replace(/\./g,'').replace(',','.'); return parseFloat(s)||0;}
function addLinha(){
  const tr=document.createElement('tr');
  let opts='<option value="">Selecione...</option>';
  PRODUTOS.forEach(p=>{opts+='<option value="'+p.id+'">'+p.nome.replace(/</g,'&lt;')+(p.controla?' (estoque: '+p.estoque+')':'')+'</option>';});
  tr.innerHTML='<td><select name="produto_id">'+opts+'</select></td>'+
    '<td><input name="qtd" type="number" min="1" value="1"></td>'+
    '<td><input name="preco" inputmode="decimal" value="0,00"></td>'+
    '<td class="n sub">R$ 0,00</td>'+
    '<td><button type="button" class="btn red sm">×</button></td>';
  tr.querySelector('select').addEventListener('change',e=>{
    const p=PRODUTOS.find(x=>x.id==e.target.value);
    tr.querySelector('[name=preco]').value=p?(p.preco/100).toFixed(2).replace('.',','):'0,00'; calc();});
  tr.querySelectorAll('input').forEach(i=>i.addEventListener('input',calc));
  tr.querySelector('button').addEventListener('click',()=>{tr.remove();calc();});
  document.querySelector('#itens tbody').appendChild(tr);
}
function calc(){
  let t=0;
  document.querySelectorAll('#itens tbody tr').forEach(tr=>{
    const s=num(tr.querySelector('[name=qtd]').value)*num(tr.querySelector('[name=preco]').value);
    tr.querySelector('.sub').textContent=fmt(s); t+=s;});
  document.getElementById('total').textContent=fmt(t);
}
addLinha();
</script>
{% endblock %}"""

VENDA_VER = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Venda #{{ v.id }} <span class="tag {{ 'er' if v.status == 'cancelada' else 'ok' }}">{{ v.status }}</span></h1>
<div class="card"><p><b>Data da conclusão:</b> {{ v.data|dt }} &nbsp; <b>Cliente:</b> {{ v.cliente or 'Consumidor' }}
{% if v.orcamento_id %}&nbsp; <b>Origem:</b> {% if orc_existe %}<a href="{{ url_for('orcamento_ver', oid=v.orcamento_id) }}">Orçamento #{{ v.orcamento_id }}</a>{% else %}Orçamento #{{ v.orcamento_id }} (excluído){% endif %}{% endif %}</p>
{% if v.obs and not v.orcamento_id %}<p class="mut">{{ v.obs }}</p>{% endif %}
{% if linhas %}<table><tr><th>Produto</th><th>Medidas</th><th class="n">Qtd.</th><th class="n">Valor unit.</th><th class="n">Total</th></tr>
{% for l in linhas %}<tr><td>{{ l.nome }}</td><td class="mut">{% if l.largura and l.altura %}{{ l.largura|num }} × {{ l.altura|num }} cm{% endif %}</td><td class="n">{{ l.qtd }}</td><td class="n">{{ l.valor_unit|brl }}</td><td class="n">{{ l.total|brl }}</td></tr>{% endfor %}</table>
{% else %}<table><tr><th>Produto</th><th class="n">Qtd</th><th class="n">Preço unit.</th><th class="n">Subtotal</th></tr>
{% for i in itens %}<tr><td>{{ i.nome }}</td><td class="n">{{ i.qtd }}</td><td class="n">{{ i.preco_unit|brl }}</td><td class="n">{{ (i.qtd * i.preco_unit)|brl }}</td></tr>{% endfor %}</table>{% endif %}
<div class="total">{% if v.desconto %}<span class="mut" style="font-size:15px">Valor bruto {{ (v.valor_bruto or v.total)|brl }} · Desconto − {{ v.desconto|brl }}</span><br>{% endif %}Valor líquido: <b>{{ v.total|brl }}</b></div>
<a class="btn sec" href="{{ url_for('vendas') }}">Voltar</a>
{% if v.status == 'ativa' %}<form class="inline" method="post" action="{{ url_for('venda_cancelar', vid=v.id) }}" onsubmit="return confirm('{{ 'Estornar a venda? O orçamento volta para Aprovado e o estoque continua baixado.' if v.orcamento_id else 'Cancelar a venda e devolver os produtos ao estoque?' }}')"><button class="btn red">{{ 'Estornar venda' if v.orcamento_id else 'Cancelar venda' }}</button></form>{% endif %}
</div>
{% if v.status == 'ativa' %}<div class="card"><h2>Recebimento</h2>
{% if v.recebido_em %}<p>Recebido em <b>{{ v.recebido_em|dia }}</b>. <span class="mut">A receita desta venda já foi contada na conclusão; registrar o recebimento não soma de novo.</span></p>
<form method="post" action="{{ url_for('venda_recebimento', vid=v.id) }}"><input type="hidden" name="desfazer" value="1"><button class="btn sec sm">Desfazer recebimento</button></form>
{% else %}<p class="mut">Esta venda conta como receita, mas ainda <b>não</b> consta como dinheiro recebido.</p>
<form method="post" action="{{ url_for('venda_recebimento', vid=v.id) }}" class="bar" style="align-items:end"><div><label>Data do recebimento</label><input type="date" name="recebido_em" value="{{ hoje }}" style="width:170px"></div><button class="btn sm">Registrar recebimento</button></form>{% endif %}</div>{% endif %}
<div class="card"><h2>Histórico da venda</h2>{% if eventos %}<table><tr><th>Data</th><th>Evento</th><th class="n">Antes</th><th class="n">Depois</th><th>Obs.</th></tr>
{% for e in eventos %}<tr><td>{{ e.data|dt }}</td><td>{{ e.evento }}</td><td class="n">{% if e.total_antes is not none %}{{ e.total_antes|brl }}{% endif %}</td><td class="n">{% if e.total_depois is not none %}{{ e.total_depois|brl }}{% endif %}</td><td class="mut">{{ e.obs or '' }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Sem eventos registrados (venda anterior a esta versão).</p>{% endif %}</div>
{% endblock %}"""

AGENDA = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Agenda</h1>
<div class="bar"><a class="btn" href="{{ url_for('agenda_form') }}">+ Novo compromisso</a>
{% if ver == 'todos' %}<a class="btn sec" href="{{ url_for('agenda') }}">Ver só em aberto</a>{% else %}<a class="btn sec" href="{{ url_for('agenda', ver='todos') }}">Ver todos</a>{% endif %}</div>
<div class="card">{% if rows %}<table><tr><th>Data e hora</th><th>Compromisso</th><th>Cliente</th><th>Situação</th><th></th></tr>
{% for a in rows %}<tr><td>{{ a.data_hora|dt }}</td><td>{{ a.titulo }}{% if a.obs %}<div class="mut">{{ a.obs }}</div>{% endif %}</td><td>{{ a.cliente or '' }}</td>
<td>{% if a.status == 'agendado' and a.data_hora < agora %}<span class="tag er">atrasado</span>{% else %}<span class="tag {{ 'ok' if a.status == 'concluido' else ('er' if a.status == 'cancelado' else '') }}">{{ a.status }}</span>{% endif %}</td>
<td class="n">
{% if a.status == 'agendado' %}<form class="inline" method="post" action="{{ url_for('agenda_status', aid=a.id) }}"><input type="hidden" name="status" value="concluido"><button class="btn sm">Concluir</button></form>
<form class="inline" method="post" action="{{ url_for('agenda_status', aid=a.id) }}"><input type="hidden" name="status" value="cancelado"><button class="btn sec sm">Cancelar</button></form>
{% else %}<form class="inline" method="post" action="{{ url_for('agenda_status', aid=a.id) }}"><input type="hidden" name="status" value="agendado"><button class="btn sec sm">Reabrir</button></form>{% endif %}
<a class="btn sec sm" href="{{ url_for('agenda_form', aid=a.id) }}">Editar</a>
<form class="inline" method="post" action="{{ url_for('agenda_excluir', aid=a.id) }}" onsubmit="return confirm('Excluir?')"><button class="btn red sm">Excluir</button></form></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum compromisso.</p>{% endif %}</div>
{% endblock %}"""

AGENDA_FORM = r"""{% extends "base.html" %}{% block conteudo %}
<h1>{{ 'Editar compromisso' if aid else 'Novo compromisso' }}</h1>
<form class="card" method="post">
<label>Título *</label><input name="titulo" value="{{ a.titulo if a else '' }}" required autofocus>
<div class="row2"><div><label>Data e hora *</label><input type="datetime-local" name="data_hora" value="{{ a.data_hora if a else '' }}" required></div>
<div><label>Cliente</label><select name="cliente_id"><option value="">Sem cliente</option>
{% for c in cl %}<option value="{{ c.id }}" {{ 'selected' if a and a.cliente_id|string == c.id|string else '' }}>{{ c.nome }}</option>{% endfor %}</select></div></div>
<label>Observações</label><textarea name="obs" rows="3">{{ a.obs if a else '' }}</textarea>
<p><button class="btn">Salvar</button> <a class="btn sec" href="{{ url_for('agenda') }}">Cancelar</a></p></form>
{% endblock %}"""

PRODUTO_MOV = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><h1>Histórico de estoque — {{ p.nome }}</h1><div class="bar noprint"><a class="btn sec" href="{{ url_for('produtos') }}">Voltar</a></div></div>
<div class="card"><p>Saldo atual: <span class="tag {{ 'er' if p.estoque <= 0 else 'ok' }}">{{ p.estoque|num(4) }} {{ p.unidade|ul }}</span></p>
{% if movs %}<table><tr><th>Data</th><th>Movimentação</th><th>Origem</th><th class="n">Quantidade</th><th class="n">Saldo após</th></tr>
{% set nomes = {'saida_orcamento': 'Saída — orçamento aprovado', 'estorno_orcamento': 'Retorno — orçamento reaberto/cancelado/excluído', 'ajuste_orcamento': 'Ajuste — orçamento aprovado e editado (só a diferença)', 'saida_venda': 'Saída — venda', 'estorno_venda': 'Retorno — venda cancelada'} %}
{% for m in movs %}<tr><td>{{ m.data|dt }}</td><td>{{ nomes.get(m.tipo, m.tipo) }}</td>
<td>{% if m.orcamento_id %}<a href="{{ url_for('orcamento_ver', oid=m.orcamento_id) }}">Orçamento #{{ m.orcamento_id }}</a>{% elif m.venda_id %}<a href="{{ url_for('venda_ver', vid=m.venda_id) }}">Venda #{{ m.venda_id }}</a>{% endif %}</td>
<td class="n">{{ m.qtd|num(4) }}</td><td class="n"><b>{{ m.saldo_apos|num(4) }}</b></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhuma movimentação registrada ainda (as movimentações passam a ser registradas a partir desta versão).</p>{% endif %}</div>
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "dashboard.html": DASHBOARD, "financeiro.html": FINANCEIRO, "empresa.html": EMPRESA, "categorias.html": CATEGORIAS, "imprimir.html": IMPRIMIR, "avaliacoes.html": AVALIACOES, "bancos.html": BANCOS, "bancos_categorias.html": BANCOS_CAT, "extrato_envio.html": EXTRATO_ENVIO, "extrato_ver.html": EXTRATO_VER, "duplicidades.html": DUPLICIDADES, "clientes.html": CLIENTES,
    "cliente_ver.html": CLIENTE_VER, "cliente_form.html": CLIENTE_FORM,
    "produtos.html": PRODUTOS, "produto_form.html": PRODUTO_FORM, "produto_mov.html": PRODUTO_MOV,
    "orcamentos.html": ORCAMENTOS, "orcamento_form.html": ORCAMENTO_FORM,
    "orcamento_ver.html": ORCAMENTO_VER, "vendas.html": VENDAS,
    "venda_nova.html": VENDA_NOVA, "venda_ver.html": VENDA_VER,
    "agenda.html": AGENDA, "agenda_form.html": AGENDA_FORM,
})

iniciar_db()

def _porta_ocupada(porta=5000):
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", porta)) == 0


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    if _porta_ocupada():                       # já está aberto: só abre o navegador
        print(f"\n  O sistema já está rodando. Abrindo {url}...")
        webbrowser.open(url)
        sys.exit(0)
    print(f"\n  {NOME_EMPRESA} rodando em {url}\n  Para encerrar, feche esta janela ou aperte Ctrl+C.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
