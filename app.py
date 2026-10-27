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
                   request, send_file, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

NOME_EMPRESA = "CIR Molduras"
# Rodando como .exe (PyInstaller) o __file__ aponta para uma pasta temporária: os dados ficam ao lado do .exe.
PASTA = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PASTA, "gestao.db")

UNIDADES = {"un": "un", "m2": "m²", "ml": "ML"}           # código -> rótulo curto
ORC = {  # status do orçamento -> (rótulo, cor)
    "orcamento": ("Orçamento", ""),
    "aberto": ("Em aberto", "wa"),
    "aprovado": ("Aprovado", "ok"),
    "producao": ("Em produção", "wa"),
    "concluido": ("Concluído / entregue", "ok"),
    "recusado": ("Cancelado / recusado", "er"),
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.secret_key = os.urandom(24)           # trocada por uma chave fixa guardada no banco (carregar_segredo)
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)


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
CREATE TABLE IF NOT EXISTS pagamentos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, orcamento_id INTEGER NOT NULL, valor INTEGER NOT NULL,
  data TEXT NOT NULL, forma TEXT, obs TEXT, criado_em TEXT,
  FOREIGN KEY(orcamento_id) REFERENCES orcamentos(id));
CREATE INDEX IF NOT EXISTS idx_pag_orc ON pagamentos(orcamento_id);
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
    if "unidade" not in {r[1] for r in con.execute("PRAGMA table_info(categorias_produto)")}:
        con.execute("ALTER TABLE categorias_produto ADD COLUMN unidade TEXT NOT NULL DEFAULT 'un'")
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
    ccols = {r[1] for r in con.execute("PRAGMA table_info(clientes)")}
    for col in ("documento", "documento_norm"):
        if col not in ccols:
            con.execute(f"ALTER TABLE clientes ADD COLUMN {col} TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cli_doc ON clientes(documento_norm) "
                "WHERE documento_norm IS NOT NULL AND documento_norm<>''")
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
    for col, tipo in (("followup_em", "TEXT"), ("followup_off", "INTEGER NOT NULL DEFAULT 0")):
        if col not in ocols:
            con.execute(f"ALTER TABLE orcamentos ADD COLUMN {col} {tipo}")
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
    if "pronto_enviado_em" not in {r[1] for r in con.execute("PRAGMA table_info(orcamentos)")}:
        con.execute("ALTER TABLE orcamentos ADD COLUMN pronto_enviado_em TEXT")
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
    con.execute("""CREATE TABLE IF NOT EXISTS mkt_resumo(id INTEGER PRIMARY KEY, loja TEXT NOT NULL, plataforma TEXT,
        ini TEXT NOT NULL, fim TEXT NOT NULL, pedidos INTEGER, valor INTEGER, validos INTEGER, valor_validos INTEGER,
        cancelados INTEGER, valor_cancelados INTEGER, clientes INTEGER, importado_em TEXT, UNIQUE(loja, ini, fim))""")
    con.executescript("""
CREATE TABLE IF NOT EXISTS mkt_produtos(id INTEGER PRIMARY KEY, ini TEXT NOT NULL, fim TEXT NOT NULL, produto TEXT, loja TEXT,
  sku TEXT, anuncio TEXT, pedidos INTEGER, unidades INTEGER, valor INTEGER, importado_em TEXT);
CREATE INDEX IF NOT EXISTS idx_mktprod_per ON mkt_produtos(ini, fim);
CREATE TABLE IF NOT EXISTS mkt_variantes(id INTEGER PRIMARY KEY, ini TEXT NOT NULL, fim TEXT NOT NULL, produto TEXT, loja TEXT,
  sku TEXT, variante TEXT, variante_id TEXT, sku_principal TEXT, anuncio TEXT, pedidos INTEGER, unidades INTEGER, valor INTEGER, importado_em TEXT);
CREATE INDEX IF NOT EXISTS idx_mktvar_per ON mkt_variantes(ini, fim);
CREATE TABLE IF NOT EXISTS mkt_diario(data TEXT PRIMARY KEY, pedidos INTEGER, valor INTEGER, validos INTEGER, valor_validos INTEGER,
  cancelados INTEGER, valor_cancelados INTEGER, clientes INTEGER, importado_em TEXT);
CREATE TABLE IF NOT EXISTS mkt_catalogo(produto_id TEXT PRIMARY KEY, sku TEXT, nome TEXT);
CREATE TABLE IF NOT EXISTS mkt_vinculos(origem TEXT PRIMARY KEY, grupo TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mkt_ignorados(chave TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS mkt_custos(chave TEXT PRIMARY KEY, custo INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mkt_taxas(loja TEXT PRIMARY KEY, comissao INTEGER NOT NULL DEFAULT 0, imposto INTEGER NOT NULL DEFAULT 0);
""")
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
    try:
        tem_senha = bool(_senha_hash(get_db()))
    except sqlite3.Error:
        tem_senha = False
    return {"empresa": emp["nome"], "emp": emp, "ORC": ORC, "tem_senha": tem_senha}


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
        "SELECT (SELECT COALESCE(SUM(valor),0) FROM pagamentos WHERE substr(data,1,?)=?) + "
        "(SELECT COALESCE(SUM(total),0) FROM vendas v WHERE v.status='ativa' AND substr(v.recebido_em,1,?)=? "
        "AND NOT EXISTS (SELECT 1 FROM pagamentos p WHERE p.orcamento_id=v.orcamento_id)) vendas, "
        "(SELECT COALESCE(SUM(valor),0) FROM lancamentos WHERE tipo='receita' AND status='recebido' AND substr(pagamento,1,?)=?) rec, "
        "(SELECT COALESCE(SUM(valor),0) FROM lancamentos WHERE tipo='despesa' AND status='pago' AND substr(pagamento,1,?)=?) desp, "
        "(SELECT COALESCE(SUM(CASE WHEN p.pago IS NOT NULL THEN MAX(v.total - p.pago, 0) "
        "WHEN v.recebido_em IS NULL THEN v.total ELSE 0 END),0) FROM vendas v LEFT JOIN "
        "(SELECT orcamento_id, SUM(valor) pago FROM pagamentos GROUP BY orcamento_id) p ON p.orcamento_id=v.orcamento_id "
        "WHERE v.status='ativa' AND substr(v.data,1,?)=?) areceber",
        (n, px) * 5).fetchone()
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


@app.route("/api/financeiro/lancamentos/<int:lid>/baixa", methods=["POST"])
def api_lancamento_baixa(lid):
    db = get_db()
    r = db.execute("SELECT * FROM lancamentos WHERE id=?", (lid,)).fetchone()
    if not r:
        return jsonify(ok=False, erros=["Lançamento não encontrado."]), 404
    dados = request.get_json(silent=True) or {}
    if dados.get("desfazer"):
        db.execute("UPDATE lancamentos SET status='pendente', pagamento=NULL WHERE id=?", (lid,))
        db.commit()
        return jsonify(ok=True)
    data = str(dados.get("data", ""))
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return jsonify(ok=False, erros=["Data inválida."]), 400
    db.execute("UPDATE lancamentos SET status=?, pagamento=? WHERE id=?",
               ("pago" if r["tipo"] == "despesa" else "recebido", data, lid))
    db.commit()
    return jsonify(ok=True)


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
    comparativo = []
    for m in range(1, 13):
        pm = periodo(ano, m)
        d = dre_periodo(db, pm)
        b = db.execute("SELECT COALESCE(SUM(CASE WHEN l.acao='receita' THEN l.valor END),0) ent, COUNT(*) n "
                       "FROM extrato_linhas l JOIN extratos e ON e.id=l.extrato_id WHERE l.aplicado=1 "
                       "AND l.acao IN ('receita','despesa') AND substr(l.data,1,7)=?" + (" AND e.banco=?" if banco else ""),
                       (pm, banco) if banco else (pm,)).fetchone()
        if d["vendas_qtd"] or d["caixa"]["vendas_recebidas"] or b["n"]:
            comparativo.append({"mes": m, "vendas": d["vendas_liq"], "recebido": d["caixa"]["vendas_recebidas"],
                                "areceber": d["caixa"]["vendas_a_receber"], "banco": b["ent"],
                                "dif": b["ent"] - d["caixa"]["vendas_recebidas"], "tem_banco": b["n"] > 0})
    extratos = db.execute("SELECT e.*, (SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id AND l.aplicado=1) ok, "
                          "(SELECT COUNT(*) FROM extrato_linhas l WHERE l.extrato_id=e.id AND l.aplicado=0 "
                          "AND l.acao NOT IN ('duplicada','retido')) pend FROM extratos e ORDER BY e.id DESC LIMIT 12").fetchall()
    return render_template("bancos.html", titulo="Bancos", ano=ano, mes=mes, meses=MESES, banco=banco, tot=tot,
                           por_banco=por_banco, por_cat=por_cat, movs=movs, cat_f=cat_f, cat_nome=cat_nome,
                           sub_ent=sub_ent, sub_sai=sub_sai, lista_bancos=lista_bancos,
                           extratos=extratos, contraparte=contraparte, saldos=saldos, comparativo=comparativo,
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
                    "WHERE status='orcamento'").fetchone()
    ap = db.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM orcamentos "
                    "WHERE status IN ('aprovado','producao','concluido') AND substr(aprovado_em,1,7)=?",
                    (mes,)).fetchone()
    abertos = db.execute("SELECT o.*, c.nome cliente FROM orcamentos o JOIN clientes c "
                         "ON c.id=o.cliente_id WHERE o.status='orcamento' "
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
    contas_ini = [r for t in ("despesa", "receita") for r in _contas_pendentes(db, t)
                  if r["dias"] is not None and r["dias"] <= 7]
    contas_ini.sort(key=lambda r: (r["dias"], r["tipo"]))
    contas_tot = {t: sum(r["valor"] for r in contas_ini if r["tipo"] == t) for t in ("despesa", "receita")}
    return render_template("dashboard.html", titulo="Início", contas_ini=contas_ini[:10],
                           contas_mais=max(0, len(contas_ini) - 10), contas_tot=contas_tot,
                           hoje_iso=datetime.now().strftime("%Y-%m-%d"), v=v, ab=ab, ap=ap, investido=investido,
                           abertos=abertos, baixo=baixo, prox=prox, ultimas=ultimas,
                           n_clientes=n_clientes, atrasados=atrasados, avisos=avisos_inicio(db),
                           aviso_backup=estado_backup(db)["aviso"])


MSG_PRONTO_PADRAO = ("Olá, {nome}! Aqui é da {empresa}. Seu pedido (nº {os}) está pronto para retirada. 😊\n"
                     "{saldo}\n\nTe esperamos!")


def _ent_dias(entrega, hoje):
    try:
        return (datetime.strptime(entrega, "%Y-%m-%d").date() - hoje).days if entrega else None
    except ValueError:
        return None


@app.route("/entregas")
def entregas():
    db = get_db()
    hoje = datetime.now().date()
    rows = []
    for o in db.execute("SELECT o.id, o.titulo, o.entrega, o.status, o.pronto_enviado_em, o.total, c.nome cliente, "
                        "c.telefone FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
                        "WHERE o.status IN ('aprovado','producao') ORDER BY o.id"):
        r = dict(o)
        r["dias"] = _ent_dias(o["entrega"], hoje)
        r["saldo"] = resumo_pagamentos(db, o["id"])["saldo"]
        r["num"] = numero_whatsapp(o["telefone"])
        rows.append(r)
    rows.sort(key=lambda r: (r["dias"] is None, r["dias"] if r["dias"] is not None else 0, r["id"]))
    return render_template("entregas.html", titulo="Entregas", rows=rows,
                           modelo=cfg_get(db, "pronto_msg", "") or MSG_PRONTO_PADRAO)


@app.route("/entregas/config", methods=["POST"])
def pronto_config():
    db = get_db()
    msg = request.form.get("msg", "").replace("\r", "").strip()
    if len(msg) > 600:
        flash("A mensagem passa de 600 caracteres.", "erro")
    else:
        cfg_set(db, "pronto_msg", "" if msg == MSG_PRONTO_PADRAO else msg); db.commit()
        flash("Mensagem salva.", "ok")
    return redirect(url_for("entregas"))


@app.route("/entregas/<int:oid>/avisar", methods=["POST"])
def pronto_enviar(oid):
    """Marca o aviso como enviado e abre o WhatsApp com a mensagem de 'pronto para retirada'."""
    db = get_db()
    o = db.execute("SELECT o.*, c.nome cliente, c.telefone FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
                   "WHERE o.id=? AND o.status IN ('aprovado','producao','concluido')", (oid,)).fetchone()
    if not o:
        abort(404)
    num = numero_whatsapp(o["telefone"])
    if not num:
        flash(f"O cliente {o['cliente']} não tem um telefone válido cadastrado.", "erro")
        return redirect(url_for("entregas"))
    saldo = resumo_pagamentos(db, oid)["saldo"]
    modelo = cfg_get(db, "pronto_msg", "") or MSG_PRONTO_PADRAO
    nome = (o["cliente"] or "").strip().split(" ")[0].title() or "tudo bem"
    texto = (modelo.replace("{nome}", nome).replace("{empresa}", empresa_dados(db)["nome"])
             .replace("{os}", f"{oid:04d}")
             .replace("{saldo}", f"Saldo a pagar na retirada: {brl(saldo)}." if saldo > 0 else "Pagamento já quitado."))
    db.execute("UPDATE orcamentos SET pronto_enviado_em=? WHERE id=?", (agora_txt(), oid))
    db.commit()
    from urllib.parse import quote
    return redirect(f"https://wa.me/{num}?text={quote(texto)}")


# -------------------------------- Contas a pagar / receber -----------------------------
def somar_meses(d, n):
    """d (date) + n meses, no mesmo dia (ou no último dia do mês, se ele não existir)."""
    import calendar
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return d.replace(year=ano, month=mes, day=min(d.day, calendar.monthrange(ano, mes)[1]))


def _contas_pendentes(db, tipo):
    """Lançamentos pendentes (e, para receber, saldos de serviços) com dias até o vencimento."""
    hoje = datetime.now().date()
    res = []
    for r in db.execute("SELECT l.*, c.nome categoria FROM lancamentos l LEFT JOIN fin_categorias c ON c.id=l.categoria_id "
                        "WHERE l.tipo=? AND l.status='pendente'", (tipo,)):
        venc = r["vencimento"] or r["data"]
        try:
            dias = (datetime.strptime(venc, "%Y-%m-%d").date() - hoje).days
        except (TypeError, ValueError):
            dias = None
        res.append({"origem": "lancamento", "id": r["id"], "tipo": tipo, "descricao": r["descricao"],
                    "categoria": r["categoria"], "valor": r["valor"], "venc": venc, "dias": dias, "pago": None,
                    "link": None})
    if tipo == "receita":
        res += saldos_a_receber(db, hoje)
    return res


def saldos_a_receber(db, hoje):
    """Serviços aprovados/concluídos com saldo ainda não pago."""
    res = []
    for o in db.execute("SELECT o.*, c.nome cliente, (SELECT COALESCE(SUM(valor),0) FROM pagamentos p "
                        "WHERE p.orcamento_id=o.id) pago FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
                        "WHERE o.status IN ('aprovado','producao','concluido')"):
        saldo = o["total"] - o["pago"]
        if saldo <= 0:
            continue
        venc = (o["entrega"] or None) if "entrega" in o.keys() else None
        try:
            dias = (datetime.strptime(venc, "%Y-%m-%d").date() - hoje).days if venc else None
        except ValueError:
            venc, dias = None, None
        res.append({"origem": "servico", "id": o["id"], "tipo": "receita",
                    "descricao": f"Serviço #{o['id']} — {o['cliente']}" + (f" (já pago {brl(o['pago'])})" if o["pago"] else ""),
                    "categoria": "Serviço", "valor": saldo, "venc": venc, "dias": dias, "pago": None,
                    "link": url_for("orcamento_ver", oid=o["id"]) + "#pagamentos"})
    return res


@app.route("/contas")
def contas():
    db = get_db()
    aba = request.args.get("aba", "pagar")
    filtro = request.args.get("filtro", "todas")
    hoje = datetime.now().date()
    if aba == "pagas":
        rows = []
        for r in db.execute("SELECT l.*, c.nome categoria FROM lancamentos l LEFT JOIN fin_categorias c ON c.id=l.categoria_id "
                            "WHERE l.status IN ('pago','recebido') AND l.vencimento IS NOT NULL "
                            "ORDER BY l.pagamento DESC, l.id DESC LIMIT 100"):
            rows.append({"origem": "lancamento", "id": r["id"], "tipo": r["tipo"], "descricao": r["descricao"],
                         "categoria": r["categoria"], "valor": r["valor"], "venc": r["vencimento"], "dias": None,
                         "pago": r["pagamento"], "link": None})
        todas = rows
    else:
        todas = _contas_pendentes(db, "despesa" if aba == "pagar" else "receita")
        rows = todas
        if filtro == "vencidas":
            rows = [r for r in todas if r["dias"] is not None and r["dias"] < 0]
        elif filtro == "semana":
            rows = [r for r in todas if r["dias"] is not None and 0 <= r["dias"] <= 7]
        elif filtro == "mes":
            px = f"{hoje:%Y-%m}"
            rows = [r for r in todas if (r["venc"] or "").startswith(px)]
        rows = sorted(rows, key=lambda r: (r["venc"] or "9999", r["id"]))
    base = todas if aba != "pagas" else []
    tot = {"vencidas": sum(r["valor"] for r in base if r["dias"] is not None and r["dias"] < 0),
           "n_vencidas": sum(1 for r in base if r["dias"] is not None and r["dias"] < 0),
           "semana": sum(r["valor"] for r in base if r["dias"] is not None and 0 <= r["dias"] <= 7),
           "n_semana": sum(1 for r in base if r["dias"] is not None and 0 <= r["dias"] <= 7),
           "aberto": sum(r["valor"] for r in base)}
    cats = db.execute("SELECT id, nome, tipo FROM fin_categorias WHERE ativo=1 ORDER BY nome").fetchall()
    return render_template("contas.html", titulo="Contas", rows=rows, aba=aba, filtro=filtro, tot=tot, cats=cats,
                           hoje=f"{hoje:%Y-%m-%d}")


@app.route("/contas/nova", methods=["POST"])
def conta_nova():
    db = get_db()
    f = request.form
    try:
        n = max(1, min(60, int(f.get("parcelas") or 1)))
        d0 = datetime.strptime(f.get("vencimento", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("Confira o vencimento e o número de parcelas.", "erro")
        return redirect(url_for("contas"))
    erros = set()
    for i in range(n):
        venc = somar_meses(d0, i).isoformat()
        d, e = _lanc_dados(db, {"tipo": f.get("tipo"), "descricao": (f.get("descricao", "").strip()
                                + (f" ({i + 1}/{n})" if n > 1 else "")), "categoria_id": f.get("categoria_id"),
                                "valor": f.get("valor"), "data": venc, "vencimento": venc, "status": "pendente"})
        if e:
            erros.update(e)
            break
        db.execute("INSERT INTO lancamentos(tipo,descricao,categoria_id,valor,data,vencimento,pagamento,status,obs,criado_em) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (d["tipo"], d["descricao"], d["categoria_id"], d["valor"], d["data"],
                                                     d["vencimento"], None, "pendente", "", agora_txt()))
    if erros:
        db.rollback()
        for e in sorted(erros):
            flash(e, "erro")
    else:
        db.commit()
        flash(f"{n} conta(s) criada(s).", "ok")
    return redirect(url_for("contas", aba="pagar" if f.get("tipo") == "despesa" else "receber"))


@app.route("/contas/<int:lid>/baixa", methods=["POST"])
def conta_baixa(lid):
    db = get_db()
    r = db.execute("SELECT * FROM lancamentos WHERE id=? AND status='pendente'", (lid,)).fetchone()
    data = request.form.get("data", "")
    volta_inicio = request.form.get("volta") == "inicio"
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        flash("Data inválida.", "erro")
        return redirect(url_for("dashboard" if volta_inicio else "contas"))
    if r:
        db.execute("UPDATE lancamentos SET status=?, pagamento=? WHERE id=?",
                   ("pago" if r["tipo"] == "despesa" else "recebido", data, lid))
        db.commit()
        flash("Baixa registrada.", "ok")
    if volta_inicio:
        return redirect(url_for("dashboard"))
    return redirect(url_for("contas", aba="pagar" if r and r["tipo"] == "despesa" else "receber"))


@app.route("/contas/<int:lid>/desfazer", methods=["POST"])
def conta_desfazer(lid):
    db = get_db()
    db.execute("UPDATE lancamentos SET status='pendente', pagamento=NULL WHERE id=? AND status IN ('pago','recebido')", (lid,))
    db.commit()
    flash("Baixa desfeita: a conta voltou para em aberto.", "ok")
    return redirect(url_for("contas", aba="pagas"))


@app.route("/contas/<int:lid>/excluir", methods=["POST"])
def conta_excluir(lid):
    db = get_db()
    db.execute("DELETE FROM lancamentos WHERE id=? AND status='pendente'", (lid,))
    db.commit()
    return redirect(request.referrer or url_for("contas"))


def avisos_inicio(db):
    """Alertas do Início: lista de (classe, texto, link)."""
    av = []
    hoje = datetime.now().date()
    pag = _contas_pendentes(db, "despesa")
    venc = [r for r in pag if r["dias"] is not None and r["dias"] < 0]
    prox = [r for r in pag if r["dias"] is not None and 0 <= r["dias"] <= 7]
    if venc:
        av.append(("erro", f"{len(venc)} conta(s) a pagar vencida(s): {brl(sum(r['valor'] for r in venc))}.",
                   url_for("contas", aba="pagar", filtro="vencidas")))
    if prox:
        av.append(("aviso", f"{len(prox)} conta(s) a pagar vencem nos próximos 7 dias: {brl(sum(r['valor'] for r in prox))}.",
                   url_for("contas", aba="pagar", filtro="semana")))
    ent_atr = ent_hoje = ent_amanha = 0
    for o in db.execute("SELECT entrega FROM orcamentos WHERE status IN ('aprovado','producao') AND entrega<>''"):
        d = _ent_dias(o["entrega"], hoje)
        if d is None:
            continue
        ent_atr += d < 0
        ent_hoje += d == 0
        ent_amanha += d == 1
    if ent_atr:
        av.insert(0, ("erro", f"{ent_atr} serviço(s) com entrega atrasada.", url_for("entregas")))
    if ent_hoje or ent_amanha:
        av.insert(0, ("aviso", f"Entregas: {ent_hoje} para hoje e {ent_amanha} para amanhã.", url_for("entregas")))
    parados = [r for r in _followup_lista(db) if r["devido"]]
    if parados:
        av.append(("aviso", f"{len(parados)} orçamento(s) sem resposta há {followup_dias(db)}+ dias "
                            f"({brl(sum(r['total'] for r in parados))}).", url_for("followup")))
    sd = [r for r in saldos_a_receber(db, hoje)]
    ent = db.execute("SELECT id FROM orcamentos WHERE status='concluido'").fetchall()
    ids = {e["id"] for e in ent}
    devendo = [r for r in sd if r["id"] in ids]
    if devendo:
        av.append(("aviso", f"{len(devendo)} serviço(s) entregue(s) com saldo a receber: "
                            f"{brl(sum(r['valor'] for r in devendo))}.", url_for("contas", aba="receber")))
    return av


# -------------------------------- Exportar para o contador -----------------------------
def _csv_txt(v):
    """Texto seguro para planilha: célula que começa com = + - @ não vira fórmula."""
    v = "" if v is None else str(v).replace("\r", " ").replace("\n", " ")
    return ("'" + v) if v[:1] in ("=", "+", "-", "@") else v


def _csv_val(c):
    return f"{(c or 0) / 100:.2f}".replace(".", ",")


def _csv(cabecalho, linhas):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(cabecalho)
    w.writerows(linhas)
    return "﻿" + buf.getvalue()


@app.route("/exportar")
def exportar():
    ano, mes = ler_periodo(request.args)
    return render_template("exportar.html", titulo="Exportar", ano=ano, mes=mes, meses=MESES)


@app.route("/exportar/baixar")
def exportar_baixar():
    import zipfile
    db = get_db()
    ano, mes = ler_periodo(request.args)
    px = periodo(ano, mes)
    n = len(px)
    arqs = {}
    arqs["vendas.csv"] = _csv(
        ["Venda", "Data", "Cliente", "Situação", "Valor bruto", "Desconto", "Total líquido", "Custo dos produtos (CPV)",
         "Recebido em", "Orçamento"],
        [[r["id"], (r["data"] or "")[:10], _csv_txt(r["cliente"] or "Consumidor"), r["status"],
          _csv_val(r["valor_bruto"] if r["valor_bruto"] is not None else r["total"]), _csv_val(r["desconto"]),
          _csv_val(r["total"]), _csv_val(r["cpv"]) if r["cpv"] is not None else "", (r["recebido_em"] or "")[:10],
          r["orcamento_id"] or ""]
         for r in db.execute("SELECT v.*, c.nome cliente FROM vendas v LEFT JOIN clientes c ON c.id=v.cliente_id "
                             "WHERE substr(v.data,1,?)=? ORDER BY v.data, v.id", (n, px))])
    arqs["financeiro.csv"] = _csv(
        ["Data", "Tipo", "Descrição", "Categoria", "Grupo", "Valor", "Vencimento", "Pagamento/recebimento", "Situação"],
        [[r["data"], r["tipo"], _csv_txt(r["descricao"]), _csv_txt(r["categoria"] or ""), r["grupo"] or "",
          _csv_val(r["valor"]), r["vencimento"] or "", r["pagamento"] or "", r["status"]]
         for r in db.execute("SELECT l.*, c.nome categoria, c.grupo FROM lancamentos l LEFT JOIN fin_categorias c "
                             "ON c.id=l.categoria_id WHERE substr(l.data,1,?)=? ORDER BY l.data, l.id", (n, px))])
    arqs["bancos.csv"] = _csv(
        ["Data", "Banco", "Conta", "Histórico", "Contraparte", "Categoria", "Tipo", "Valor"],
        [[r["data"], r["banco"], r["conta"], _csv_txt(r["historico"]),
          _csv_txt(contraparte((r["detalhe"] or "").split("\n"))),
          _csv_txt(r["cat"] or ""), {"receita": "Entrada", "despesa": "Saída", "ignorar": "Ignorado (transferência própria)"}[r["acao"]],
          _csv_val(r["valor"] if r["sinal"] == "C" else -r["valor"])]
         for r in db.execute("SELECT l.*, e.banco, e.conta, c.nome cat FROM extrato_linhas l JOIN extratos e "
                             "ON e.id=l.extrato_id LEFT JOIN banco_categorias c ON c.id=l.categoria_id "
                             "WHERE l.aplicado=1 AND l.acao IN ('receita','despesa','ignorar') AND substr(l.data,1,?)=? "
                             "ORDER BY l.data, l.id", (n, px))])
    arqs["pagamentos_servicos.csv"] = _csv(
        ["Data", "Serviço", "Cliente", "Forma", "Obs.", "Valor"],
        [[r["data"], r["orcamento_id"], _csv_txt(r["cliente"]), r["forma"] or "", _csv_txt(r["obs"] or ""), _csv_val(r["valor"])]
         for r in db.execute("SELECT p.*, c.nome cliente FROM pagamentos p JOIN orcamentos o ON o.id=p.orcamento_id "
                             "JOIN clientes c ON c.id=o.cliente_id WHERE substr(p.data,1,?)=? ORDER BY p.data, p.id", (n, px))])
    d = dre_periodo(db, px)
    arqs["resumo.csv"] = _csv(["Item", "Valor"], [
        ["Receita bruta de vendas", _csv_val(d["receita_bruta"])], ["(-) Descontos", _csv_val(d["descontos"])],
        ["(-) Deduções", _csv_val(d["deducoes"])], ["(=) Receita líquida", _csv_val(d["receita_liquida"])],
        ["(-) Custo dos produtos vendidos", _csv_val(d["cpv"])], ["(=) Lucro bruto", _csv_val(d["lucro_bruto"])],
        ["(-) Despesas operacionais", _csv_val(d["despesas_operacionais"])],
        ["(=) Resultado operacional", _csv_val(d["resultado_operacional"])],
        ["(+/-) Outras receitas e despesas", _csv_val(d["outras_liquido"])],
        ["(=) Resultado líquido gerencial", _csv_val(d["resultado_liquido"])],
        ["Caixa: vendas recebidas", _csv_val(d["caixa"]["vendas_recebidas"])],
        ["Caixa: outras receitas recebidas", _csv_val(d["caixa"]["receitas_recebidas"])],
        ["Caixa: despesas pagas", _csv_val(d["caixa"]["despesas_pagas"])],
        ["Caixa: saldo do período", _csv_val(d["caixa"]["saldo"])],
        ["Vendas ainda a receber", _csv_val(d["caixa"]["vendas_a_receber"])],
        ["Aviso", "Custo incompleto em alguma venda: lucro parcial" if d["cpv_incompleto"] else ""]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, conteudo in arqs.items():
            z.writestr(nome, conteudo.encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"contador_{px.replace('-', '_')}.zip")


# -------------------------------- Relatórios -----------------------------
@app.route("/relatorios")
def relatorios():
    db = get_db()
    ano, mes = ler_periodo(request.args)
    px = periodo(ano, mes)
    dias = max(15, min(1000, inteiro(request.args.get("dias"), 90)))
    prods = {}
    for r in db.execute(
            "SELECT oi.produto_id pid, COALESCE(p.nome, oi.descricao) nome, SUM(oi.qtd) qtd, SUM(oi.subtotal) receita "
            "FROM orcamento_itens oi JOIN vendas v ON v.orcamento_id=oi.orcamento_id AND v.status='ativa' "
            "LEFT JOIN produtos p ON p.id=oi.produto_id WHERE substr(v.data,1,?)=? "
            "GROUP BY oi.produto_id, COALESCE(p.nome, oi.descricao)", (len(px), px)):
        prods[(r["pid"], r["nome"])] = {"nome": r["nome"], "qtd": r["qtd"] or 0, "receita": r["receita"] or 0,
                                        "custo": None, "pid": r["pid"]}
    for r in db.execute(
            "SELECT ii.produto_id pid, p.nome nome, SUM(ii.qtd) qtd, SUM(ii.qtd*ii.preco_unit) receita "
            "FROM itens_venda ii JOIN vendas v ON v.id=ii.venda_id AND v.status='ativa' AND v.orcamento_id IS NULL "
            "JOIN produtos p ON p.id=ii.produto_id WHERE substr(v.data,1,?)=? GROUP BY ii.produto_id", (len(px), px)):
        d = prods.setdefault((r["pid"], r["nome"]), {"nome": r["nome"], "qtd": 0, "receita": 0, "custo": None, "pid": r["pid"]})
        d["qtd"] += r["qtd"] or 0
        d["receita"] += r["receita"] or 0
    for r in db.execute(
            "SELECT vc.produto_id pid, SUM(vc.subtotal) custo FROM venda_custos vc JOIN vendas v ON v.id=vc.venda_id "
            "AND v.status='ativa' WHERE vc.conhecido=1 AND vc.produto_id IS NOT NULL AND substr(v.data,1,?)=? "
            "GROUP BY vc.produto_id", (len(px), px)):
        for k, d in prods.items():
            if d["pid"] == r["pid"]:
                d["custo"] = r["custo"]
    lista = sorted(prods.values(), key=lambda d: -d["receita"])[:20]
    for d in lista:
        d["margem"] = (d["receita"] - d["custo"]) if d["custo"] is not None else None
    clientes = db.execute(
        "SELECT c.id, c.nome, COUNT(*) n, SUM(v.total) total FROM vendas v JOIN clientes c ON c.id=v.cliente_id "
        "WHERE v.status='ativa' AND substr(v.data,1,?)=? GROUP BY c.id ORDER BY total DESC LIMIT 10", (len(px), px)).fetchall()
    hoje = datetime.now().date()
    inativos = []
    from urllib.parse import quote
    emp = empresa_dados(db)["nome"]
    for c in db.execute(
            "SELECT c.id, c.nome, c.telefone, MAX(v.data) ultima, SUM(v.total) total FROM vendas v JOIN clientes c "
            "ON c.id=v.cliente_id WHERE v.status='ativa' GROUP BY c.id ORDER BY ultima"):
        try:
            ult = datetime.strptime(c["ultima"][:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        d = (hoje - ult).days
        if d >= dias:
            num = numero_whatsapp(c["telefone"])
            nome = (c["nome"] or "").split(" ")[0].title()
            txt = f"Olá, {nome}! Aqui é da {emp}. Faz um tempo que não nos vemos. Precisando de uma moldura, um quadro ou um conserto, é só chamar! 😊"
            inativos.append({"id": c["id"], "nome": c["nome"], "telefone": c["telefone"], "ultima": c["ultima"][:10],
                             "dias_sem": d, "total": c["total"],
                             "wa": f"https://wa.me/{num}?text={quote(txt)}" if num else ""})
    return render_template("relatorios.html", titulo="Relatórios", ano=ano, mes=mes, meses=MESES, dias=dias,
                           prods=lista, clientes=clientes, inativos=inativos[:50])


# -------------------------------- Marketplaces (UpSeller) ----------------------
def ler_xlsx(dados):
    """Lê a primeira planilha de um .xlsx (sem bibliotecas externas). Devolve lista de linhas."""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(io.BytesIO(dados))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            ss.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    nome = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    linhas = []
    for row in ET.fromstring(z.read(nome)).iter("{%s}row" % ns["m"]):
        cels = {}
        for c in row.findall("m:c", ns):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            t = c.get("t")
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            else:
                ve = c.find("m:v", ns)
                v = ve.text if ve is not None else None
                if v is not None and t == "s":
                    v = ss[int(v)]
            cels[idx - 1] = v
        linhas.append([cels.get(i) for i in range(max(cels) + 1)] if cels else [])
    return linhas


def _num_c(v):
    try:
        return int((Decimal(str(v or 0)) * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    except InvalidOperation:
        return 0


def _num_i(v):
    try:
        return int(Decimal(str(v or 0)))
    except InvalidOperation:
        return 0


def mkt_loja(nome):
    """'Cir shopee[Shopee BR]' -> 'Cir shopee'."""
    return re.sub(r"\s*\[.*\]\s*$", "", str(nome or "")).strip()


def _data_xlsx(v):
    """Data do Excel: número de série (46295), 'AAAA-MM-DD' ou 'DD/MM/AAAA' -> 'AAAA-MM-DD' (ou None)."""
    from datetime import timedelta
    t = str(v or "").strip()
    try:
        if re.fullmatch(r"\d+(\.\d+)?", t):
            return (datetime(1899, 12, 30) + timedelta(days=int(float(t)))).strftime("%Y-%m-%d")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", t):
            return datetime.strptime(t[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", t):
            return datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return None


def _importar_mkt_diario(linhas):
    cab = [str(c or "").strip().lower() for c in linhas[0]]
    def ci(*ns):
        for n in ns:
            if n in cab:
                return cab.index(n)
        return None
    c = dict(ped=ci("total de pedidos"), val=ci("valor total de vendas"), vp=ci("pedidos válidos", "pedidos validos"),
             vv=ci("valor de vendas válidas", "valor de vendas validas"), cp=ci("pedidos cancelados"),
             cv=ci("valor de vendas canceladas"), cli=ci("clientes"))
    db = get_db()
    n, datas = 0, []
    for l in linhas[1:]:
        d = _data_xlsx(l[0] if l else None)
        if not d:
            continue
        g = lambda k: l[c[k]] if c[k] is not None and c[k] < len(l) else None
        db.execute("""INSERT INTO mkt_diario(data,pedidos,valor,validos,valor_validos,cancelados,valor_cancelados,clientes,importado_em)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(data) DO UPDATE SET pedidos=excluded.pedidos,valor=excluded.valor,
            validos=excluded.validos,valor_validos=excluded.valor_validos,cancelados=excluded.cancelados,
            valor_cancelados=excluded.valor_cancelados,clientes=excluded.clientes,importado_em=excluded.importado_em""",
                   (d, _num_i(g("ped")), _num_c(g("val")), _num_i(g("vp")), _num_c(g("vv")), _num_i(g("cp")), _num_c(g("cv")),
                    _num_i(g("cli")), agora_txt()))
        n += 1
        datas.append(d)
    db.commit()
    if not n:
        flash("Não encontrei datas válidas nesse relatório diário.", "erro")
        return redirect(url_for("marketplaces_dados"))
    a, b = min(datas), max(datas)
    flash(f"Relatório diário importado: {n} dia(s), de {a[8:]}/{a[5:7]} a {b[8:]}/{b[5:7]}. Dias já importados foram atualizados, sem duplicar.", "ok")
    return redirect(url_for("marketplaces", dm=b[:7]))


def _importar_mkt_variantes(linhas, cab, ini, fim):
    nomes = ("produtos", "loja", "sku", "variantes", "id da variante", "sku principal", "id do anúncios",
             "pedidos válidos", "unidades vendidas", "valor de vendas")
    ix = {n: cab.index(n) for n in nomes if n in cab}
    if "valor de vendas" not in ix:
        flash("Faltam colunas no relatório de variantes (Valor de Vendas).", "erro")
        return redirect(url_for("marketplaces_dados"))
    def g(l, k):
        i = ix.get(k)
        return l[i] if i is not None and i < len(l) else None
    db = get_db()
    db.execute("DELETE FROM mkt_variantes WHERE ini=? AND fim=?", (ini, fim))
    n = 0
    for l in linhas[1:]:
        nome = str(g(l, "produtos") or "").strip()
        if not nome:
            continue
        un, val = _num_i(g(l, "unidades vendidas")), _num_c(g(l, "valor de vendas"))
        if un <= 0 and val <= 0:
            continue
        db.execute("""INSERT INTO mkt_variantes(ini,fim,produto,loja,sku,variante,variante_id,sku_principal,anuncio,pedidos,unidades,valor,importado_em)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (ini, fim, nome, mkt_loja(g(l, "loja")), _idn(g(l, "sku")), str(g(l, "variantes") or "").strip(),
                    _idn(g(l, "id da variante")), _idn(g(l, "sku principal")), _idn(g(l, "id do anúncios")),
                    _num_i(g(l, "pedidos válidos")), un, val, agora_txt()))
        n += 1
    db.commit()
    flash(f"{n} variação(ões) importada(s) para {ini[8:]}/{ini[5:7]} a {fim[8:]}/{fim[5:7]}. Preencha o custo de cada variação.", "ok")
    return redirect(url_for("marketplaces", aba="lucro", p=f"{ini}|{fim}"))


def _importar_mkt_produtos(linhas, cab, ini, fim):
    ix = {n: cab.index(n) for n in ("produtos", "loja", "sku principal", "id do anúncios", "pedidos válidos",
                                    "unidades vendidas", "valor de vendas") if n in cab}
    if "valor de vendas" not in ix:
        flash("Faltam colunas no relatório de produtos (Valor de Vendas).", "erro")
        return redirect(url_for("marketplaces_dados"))
    def g(l, k):
        i = ix.get(k)
        return l[i] if i is not None and i < len(l) else None
    db = get_db()
    db.execute("DELETE FROM mkt_produtos WHERE ini=? AND fim=?", (ini, fim))
    n = 0
    for l in linhas[1:]:
        nome = str(g(l, "produtos") or "").strip()
        if not nome:
            continue
        db.execute("INSERT INTO mkt_produtos(ini,fim,produto,loja,sku,anuncio,pedidos,unidades,valor,importado_em) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (ini, fim, nome, mkt_loja(g(l, "loja")), _idn(g(l, "sku principal")),
                    _idn(g(l, "id do anúncios")), _num_i(g(l, "pedidos válidos")),
                    _num_i(g(l, "unidades vendidas")), _num_c(g(l, "valor de vendas")), agora_txt()))
        n += 1
    db.commit()
    flash(f"{n} produto(s) importado(s) para {ini[8:]}/{ini[5:7]} a {fim[8:]}/{fim[5:7]}. Preencha o custo e as taxas para ver o lucro.", "ok")
    return redirect(url_for("marketplaces", aba="lucro", p=f"{ini}|{fim}"))


def _bp(v):
    """'12,5' -> 1250 (centésimos de %). None se inválido ou fora de 0-100."""
    try:
        d = Decimal(str(v).replace("%", "").replace(",", ".").strip() or "0")
    except InvalidOperation:
        return None
    if d < 0 or d > 100:
        return None
    return int((d * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def _idn(v):
    """Normaliza ID/SKU vindo do Excel: '1649.0' -> '1649', '2.0297405553E10' -> '20297405553'."""
    t = str(v if v is not None else "").strip()
    if re.fullmatch(r"\d+\.0+", t):
        t = t.split(".")[0]
    elif re.fullmatch(r"\d(\.\d+)?[eE]\+?\d+", t):
        try:
            t = str(int(Decimal(t)))
        except InvalidOperation:
            pass
    return t


def _importar_mkt_catalogo(linhas, hi):
    cab = [str(c or "").strip().lower() for c in linhas[hi]]
    ci = cab.index("id do produto")
    cs = cab.index("sku de referência") if "sku de referência" in cab else None
    cn = cab.index("nome do produto") if "nome do produto" in cab else None
    db = get_db()
    db.execute("DELETE FROM mkt_catalogo")
    n = 0
    for l in linhas[hi + 1:]:
        pid = _idn(l[ci] if ci < len(l) else "")
        if not pid:
            continue
        sku = _idn(l[cs]) if cs is not None and cs < len(l) else ""
        nome = str(l[cn] or "").strip() if cn is not None and cn < len(l) else ""
        db.execute("INSERT OR REPLACE INTO mkt_catalogo(produto_id, sku, nome) VALUES(?,?,?)", (pid, sku, nome))
        n += 1
    db.commit()
    flash(f"Catálogo de referência carregado: {n} produto(s). As vendas agora são agrupadas por eles.", "ok")
    return redirect(url_for("marketplaces", aba="lucro"))


def _nome_norm(t):
    t = re.sub(r"[^a-z0-9 ]+", " ", str(t or "").lower())
    return " ".join(t.split())


def mkt_resolver(db):
    """Devolve (func, grupos): func(sku, anuncio) -> (chave_grupo, origem_do_vinculo) ou (None, None)."""
    cat = [dict(r) for r in db.execute("SELECT * FROM mkt_catalogo")]
    por_id = {c["produto_id"]: c for c in cat}
    skus = {c["sku"] for c in cat if c["sku"]}
    manual = {r["origem"]: r["grupo"] for r in db.execute("SELECT origem, grupo FROM mkt_vinculos")}
    grupos = {}
    for c in cat:
        k = c["sku"] or "id:" + c["produto_id"]
        g = grupos.setdefault(k, {"chave": k, "nomes": [], "ids": []})
        g["nomes"].append(c["nome"])
        g["ids"].append(c["produto_id"])
    def res(sku, anuncio):
        o_a, o_s = "a:" + anuncio if anuncio else None, "s:" + sku if sku else None
        for o in (o_a, o_s):
            if o and o in manual and manual[o] in grupos:
                return manual[o], "manual"
        if anuncio in por_id:
            c = por_id[anuncio]
            return (c["sku"] or "id:" + c["produto_id"]), "auto"
        if sku and sku in skus:
            return sku, "auto"
        if sku and "--" in sku and sku.split("--")[0] in skus:
            return sku.split("--")[0], "auto"
        return None, None
    return res, grupos


def mkt_calcular(db, ini, fim):
    """Lucro por produto padrão (catálogo), em centavos inteiros. Variações/lojas são somadas num produto só."""
    custos = {r["chave"]: r["custo"] for r in db.execute("SELECT chave, custo FROM mkt_custos")}
    cad = {}
    for r in db.execute("SELECT sku, custo FROM produtos WHERE sku IS NOT NULL AND sku<>'' AND custo>0"):
        cad.setdefault(r["sku"].strip(), r["custo"])
    taxas = {r["loja"]: (r["comissao"], r["imposto"]) for r in db.execute("SELECT * FROM mkt_taxas")}
    ign = {r["chave"] for r in db.execute("SELECT chave FROM mkt_ignorados")}
    try:
        mb = int(cfg_get(db, "mkt_margem", "3000"))                  # margem desejada, em centésimos de %
    except ValueError:
        mb = 3000
    res, grupos = mkt_resolver(db)
    def pc(valor, bp):
        return int((Decimal(valor) * bp / 10000).quantize(Decimal("1"), ROUND_HALF_UP))
    ag, lojas, ignorados, sem_vinc_d = {}, {}, {}, {}
    # SKU de variação que, numa mesma loja, serve para variações diferentes (ex.: Shein usa o SKU do pai em todos os tamanhos)
    # não pode ser a chave do custo: cada variação passa a ter custo próprio (pelo ID da variante).
    _lab = {}
    for r in db.execute("SELECT sku, loja, variante, variante_id FROM mkt_variantes WHERE sku<>''"):
        _lab.setdefault((_idn(r["sku"]), r["loja"]), set()).add((r["variante_id"] or r["variante"]).strip().lower())
    ambiguos = {k[0] for k, v in _lab.items() if len(v) > 1}
    fonte = [dict(r, _var=True) for r in db.execute("SELECT * FROM mkt_variantes WHERE ini=? AND fim=? ORDER BY valor DESC", (ini, fim))]
    if not fonte:
        fonte = [dict(r, _var=False) for r in db.execute("SELECT * FROM mkt_produtos WHERE ini=? AND fim=? ORDER BY valor DESC", (ini, fim))]
    for d in fonte:
        if d["_var"]:
            d["sku_var"] = _idn(d["sku"])
            d["sku"] = _idn(d["sku_principal"])
        sku, anu = _idn(d["sku"]), _idn(d["anuncio"])
        chave, como = res(sku, anu)
        d["origem"] = ("a:" + anu) if anu else (("s:" + sku) if sku else "n:" + _nome_norm(d["produto"])[:60])
        if chave is None:
            chave = sku or ("a:" + anu if anu else "n:" + _nome_norm(d["produto"])[:60])
            d["vinculado"] = False
        else:
            d["vinculado"] = True
        d["grupo_como"] = como
        if chave in ign:
            x = ignorados.setdefault(chave, {"chave": chave, "nome": d["produto"], "valor": 0})
            x["valor"] += d["valor"]
            continue
        g = ag.setdefault(chave, {"chave": chave, "fontes": [], "unidades": 0, "pedidos": 0, "valor": 0, "custo": 0,
                                  "comissao": 0, "imposto": 0, "lojas": set()})
        g["fontes"].append(d)
        g["unidades"] += d["unidades"]; g["pedidos"] += d["pedidos"]; g["valor"] += d["valor"]
        g["lojas"].add(d["loja"])
        if not d["vinculado"]:
            sv = sem_vinc_d.setdefault(d["origem"], dict(d, valor=0))
            sv["valor"] += d["valor"]
    sem_vinc = list(sem_vinc_d.values())
    T = dict(receita=0, custo=0, comissao=0, imposto=0, lucro=0, sem_custo=0)
    rows = []
    for chave, g in ag.items():
        cu = custos[chave] if chave in custos else cad.get(chave, 0)     # custo padrão do produto
        g["fontes"].sort(key=lambda f: -f["valor"])
        falta = False
        for f in g["fontes"]:
            cb, ib = taxas.get(f["loja"], (0, 0))
            if f["_var"]:
                unico = f["variante_id"] or f["origem"] + "|" + f["variante"]
                vk = "v:" + (unico if (not f["sku_var"] or f["sku_var"] in ambiguos) else f["sku_var"])
                f["rotulo"] = f["variante"] or f["produto"]
                f["sub"] = " · ".join(x for x in (f["sku_var"], f["anuncio"]) if x)
            else:
                vk = "v:" + f["origem"]
                f["rotulo"] = f["produto"]
                f["sub"] = f["sku"] or f["anuncio"]
            # variação de quantidade: "Roxo Lavanda,5" = pacote com 5 peças -> custo da peça x 5
            mq = re.search(r",\s*(\d{1,3})\s*$", f["variante"]) if f["_var"] else None
            f["qtd"] = max(int(mq.group(1)), 1) if mq else 1
            do_cad = cad.get(f.get("sku_var") or "", 0)
            f["custo_chave"] = vk
            antigo = "v:" + f["sku_var"] if f["_var"] and f["sku_var"] else None    # chave usada antes da separação
            if vk not in custos and f["_var"] and antigo and antigo != vk and antigo in custos:
                custos[vk] = custos[antigo]                                            # migra o valor que você já tinha digitado
            f["custo_fmt"] = dinheiro(custos[vk]) if vk in custos else ""
            if vk in custos:
                f["custo_base"] = custos[vk]
                f["custo_unit"] = custos[vk] * f["qtd"]
            elif do_cad:
                f["custo_base"] = f["custo_unit"] = do_cad            # custo cadastrado exatamente para este SKU
            else:
                f["custo_base"] = cu
                f["custo_unit"] = cu * f["qtd"]
            f["custo_herdado"] = dinheiro(cu) if cu else ""
            f["custo"] = f["custo_unit"] * f["unidades"]
            f["comissao"], f["imposto"] = pc(f["valor"], cb), pc(f["valor"], ib)
            f["lucro"] = f["valor"] - f["custo"] - f["comissao"] - f["imposto"]
            f["margem"] = f["lucro"] * 100.0 / f["valor"] if f["valor"] else 0
            f["preco_atual"] = f["valor"] // f["unidades"] if f["unidades"] else 0
            den = 10000 - cb - ib - mb                                  # preço mínimo = custo / (1 - comissão - imposto - margem)
            f["preco_min"] = int((Decimal(f["custo_unit"]) * 10000 / den).to_integral_value(ROUND_CEILING)) if (den > 0 and f["custo_unit"]) else None
            f["abaixo"] = bool(f["preco_min"] and f["unidades"] and f["preco_atual"] < f["preco_min"])
            g["comissao"] += f["comissao"]
            g["imposto"] += f["imposto"]
            g["custo"] += f["custo"]
            falta = falta or not f["custo_unit"]
            lojas[f["loja"]] = lojas.get(f["loja"], 0) + f["valor"]
        g["custo_unit"], g["custo_fmt"] = cu, (dinheiro(cu) if cu else "")
        g["varia"] = len({f["custo_base"] for f in g["fontes"]}) > 1
        g["falta_custo"] = falta
        g["lucro"] = g["valor"] - g["custo"] - g["comissao"] - g["imposto"]
        g["margem"] = g["lucro"] * 100.0 / g["valor"] if g["valor"] else 0
        g["preco_atual"] = g["valor"] // g["unidades"] if g["unidades"] else 0
        if g["unidades"] and all(f["preco_min"] for f in g["fontes"]):
            g["preco_min"] = sum(f["preco_min"] * f["unidades"] for f in g["fontes"]) // g["unidades"]
        else:
            g["preco_min"] = None
        g["abaixo"] = bool(g["preco_min"] and g["preco_atual"] < g["preco_min"])
        g["n_abaixo"] = sum(1 for f in g["fontes"] if f["abaixo"])
        if chave in grupos:
            nomes = grupos[chave]["nomes"]
            ids = grupos[chave]["ids"]
            melhor = max(g["fontes"], key=lambda f: f["valor"])
            g["produto"] = next((n for i, n in zip(ids, nomes) if i == _idn(melhor["anuncio"])), nomes[0]) or chave
            g["padrao"] = True
        else:
            g["produto"] = max(g["fontes"], key=lambda f: f["valor"])["produto"]
            g["padrao"] = False
        g["lojas"] = sorted(g["lojas"])
        g["sku"] = chave if not chave.startswith(("id:", "a:", "n:")) else ""
        rows.append(g)
        for k in ("custo", "comissao", "imposto", "lucro"):
            T[k] += g[k]
        T["receita"] += g["valor"]
        T["sem_custo"] += 1 if g["falta_custo"] else 0
    rows.sort(key=lambda x: -x["valor"])
    T["margem"] = T["lucro"] * 100.0 / T["receita"] if T["receita"] else 0
    T["markup"] = T["lucro"] * 100.0 / T["custo"] if T["custo"] else 0
    lojas_t = [dict(loja=l, receita=v, comissao=_fmt_bp(taxas.get(l, (0, 0))[0]), imposto=_fmt_bp(taxas.get(l, (0, 0))[1]))
               for l, v in sorted(lojas.items(), key=lambda x: -x[1])]
    # sugestões de vínculo por semelhança de nome (só sugere; quem confirma é você)
    opcoes = []
    for k, g in grupos.items():
        opcoes.append({"chave": k, "nome": (g["nomes"][0] or k)[:70], "n": _nome_norm(g["nomes"][0])})
    import difflib
    for f in sem_vinc:
        n, best, sc = _nome_norm(f["produto"]), None, 0
        for o in opcoes:
            s_ = difflib.SequenceMatcher(None, n, o["n"]).ratio()
            if s_ > sc:
                best, sc = o, s_
        f["sugestao"] = best["chave"] if best and sc >= 0.6 else ""
    T["fonte_var"] = bool(fonte and fonte[0]["_var"])
    T["margem_alvo"] = _fmt_bp(mb) or "0"
    T["n_abaixo"] = sum(1 for g in rows if g["abaixo"] or g["n_abaixo"])
    return rows, T, lojas_t, dict(sem_vinc=sem_vinc, opcoes=sorted(opcoes, key=lambda o: o["nome"]),
                                  ignorados=list(ignorados.values()), n_cat=len(grupos))


def _fmt_bp(bp):
    return (f"{bp / 100:.2f}".rstrip("0").rstrip(".") if bp else "").replace(".", ",")


@app.route("/api/marketplaces/custo", methods=["POST"])
def api_mkt_custo():
    d = request.get_json(silent=True) or {}
    chave = str(d.get("chave", "")).strip()
    if not chave:
        return jsonify(ok=False, erros=["Produto sem SKU."]), 400
    txt = str(d.get("custo", "")).strip()
    db = get_db()
    if txt == "":
        db.execute("DELETE FROM mkt_custos WHERE chave=?", (chave,))
    else:
        c = para_centavos(txt)
        if c is None or c < 0:
            return jsonify(ok=False, erros=["Custo inválido."]), 400
        db.execute("INSERT INTO mkt_custos(chave,custo) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET custo=excluded.custo", (chave, c))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/marketplaces/margem", methods=["POST"])
def api_mkt_margem():
    d = request.get_json(silent=True) or {}
    bp = _bp(d.get("valor", ""))
    if bp is None or bp >= 10000:
        return jsonify(ok=False, erros=["Informe uma margem entre 0 e 99,99%."]), 400
    db = get_db()
    cfg_set(db, "mkt_margem", str(bp))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/marketplaces/taxa", methods=["POST"])
def api_mkt_taxa():
    d = request.get_json(silent=True) or {}
    loja, campo = str(d.get("loja", "")).strip(), d.get("campo")
    bp = _bp(d.get("valor", ""))
    if not loja or campo not in ("comissao", "imposto") or bp is None:
        return jsonify(ok=False, erros=["Informe uma porcentagem entre 0 e 100."]), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO mkt_taxas(loja) VALUES(?)", (loja,))
    db.execute(f"UPDATE mkt_taxas SET {campo}=? WHERE loja=?", (bp, loja))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/marketplaces/excluir", methods=["POST"])
def api_mkt_excluir():
    d = request.get_json(silent=True) or {}
    chave = str(d.get("chave", "")).strip()
    if not chave:
        return jsonify(ok=False, erros=["Produto inválido."]), 400
    db = get_db()
    if d.get("restaurar"):
        db.execute("DELETE FROM mkt_ignorados WHERE chave=?", (chave,))
    else:
        db.execute("INSERT OR IGNORE INTO mkt_ignorados(chave) VALUES(?)", (chave,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/marketplaces/vinculo", methods=["POST"])
def api_mkt_vinculo():
    d = request.get_json(silent=True) or {}
    db = get_db()
    pares = d.get("pares") if isinstance(d.get("pares"), list) else [d]
    for p in pares:
        origem, grupo = str(p.get("origem", "")).strip(), str(p.get("grupo", "")).strip()
        if not origem:
            return jsonify(ok=False, erros=["Origem inválida."]), 400
        if not grupo:
            db.execute("DELETE FROM mkt_vinculos WHERE origem=?", (origem,))
        else:
            if not db.execute("SELECT 1 FROM mkt_catalogo WHERE sku=? OR 'id:'||produto_id=?", (grupo, grupo)).fetchone():
                return jsonify(ok=False, erros=["Produto de referência não encontrado."]), 400
            db.execute("INSERT INTO mkt_vinculos(origem,grupo) VALUES(?,?) ON CONFLICT(origem) DO UPDATE SET grupo=excluded.grupo", (origem, grupo))
    db.commit()
    return jsonify(ok=True)


@app.route("/marketplaces/limpar-catalogo", methods=["POST"])
def marketplaces_limpar_catalogo():
    db = get_db()
    db.execute("DELETE FROM mkt_catalogo")
    db.execute("DELETE FROM mkt_vinculos")
    db.commit()
    flash("Catálogo de referência removido.", "ok")
    return redirect(url_for("marketplaces_dados"))


@app.route("/marketplaces/excluir-produtos", methods=["POST"])
def marketplaces_excluir_produtos():
    db = get_db()
    for tb in ("mkt_produtos", "mkt_variantes"):
        db.execute(f"DELETE FROM {tb} WHERE ini=? AND fim=?", (request.form.get("ini", ""), request.form.get("fim", "")))
    db.commit()
    flash("Período excluído.", "ok")
    return redirect(url_for("marketplaces", aba="lucro"))


@app.route("/marketplaces/importar", methods=["POST"])
def marketplaces_importar():
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        flash("Escolha o arquivo .xlsx do UpSeller.", "erro")
        return redirect(url_for("marketplaces_dados"))
    try:
        linhas = ler_xlsx(arq.read())
    except Exception:
        flash("Não consegui ler esse arquivo. Use o .xlsx exportado pelo UpSeller.", "erro")
        return redirect(url_for("marketplaces_dados"))
    for hi in range(min(6, len(linhas))):
        if "id do produto" in [str(c or "").strip().lower() for c in linhas[hi]]:
            return _importar_mkt_catalogo(linhas, hi)
    if linhas and [str(c or "").strip().lower() for c in linhas[0]][:1] == ["data"] and \
            "valor de vendas válidas" in [str(c or "").strip().lower() for c in linhas[0]]:
        return _importar_mkt_diario(linhas)
    ini = fim = None
    m = re.search(r"(\d{8})-(\d{8})", arq.filename)
    if m:
        try:
            ini = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
            fim = datetime.strptime(m.group(2), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            ini = fim = None
    if request.form.get("ini") and request.form.get("fim"):
        ini, fim = request.form["ini"], request.form["fim"]
    if not ini or not fim or ini > fim:
        flash("Não consegui descobrir o período pelo nome do arquivo. Informe início e fim.", "erro")
        return redirect(url_for("marketplaces_dados"))
    if not linhas:
        flash("Arquivo vazio.", "erro")
        return redirect(url_for("marketplaces_dados"))
    cab = [str(c or "").strip().lower() for c in linhas[0]]
    if "unidades vendidas" in cab and "variantes" in cab and "id da variante" in cab:
        return _importar_mkt_variantes(linhas, cab, ini, fim)
    if "unidades vendidas" in cab and "produtos" in cab:
        return _importar_mkt_produtos(linhas, cab, ini, fim)
    def col(*nomes):
        for n in nomes:
            if n in cab:
                return cab.index(n)
        return None
    c = {"loja": col("loja"), "plat": col("plataforma"), "ped": col("total de pedidos"),
         "val": col("valor total de vendas"), "vp": col("pedidos válidos", "pedidos validos"),
         "vv": col("valor de vendas válidas", "valor de vendas validas"),
         "cp": col("pedidos cancelados"), "cv": col("valor de vendas canceladas"), "cli": col("clientes")}
    if c["loja"] is None or c["vv"] is None or c["vp"] is None:
        flash("Este não parece o relatório “Vendas loja performance” (faltam as colunas Loja / Pedidos Válidos / Valor de Vendas Válidas).", "erro")
        return redirect(url_for("marketplaces_dados"))
    def g(l, k):
        i = c[k]
        return l[i] if i is not None and i < len(l) else None
    db = get_db()
    n = 0
    for l in linhas[1:]:
        loja = str(g(l, "loja") or "").strip()
        if not loja:
            continue
        db.execute("""INSERT INTO mkt_resumo(loja,plataforma,ini,fim,pedidos,valor,validos,valor_validos,cancelados,valor_cancelados,clientes,importado_em)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(loja,ini,fim) DO UPDATE SET plataforma=excluded.plataforma,
            pedidos=excluded.pedidos,valor=excluded.valor,validos=excluded.validos,valor_validos=excluded.valor_validos,
            cancelados=excluded.cancelados,valor_cancelados=excluded.valor_cancelados,clientes=excluded.clientes,importado_em=excluded.importado_em""",
            (loja, str(g(l, "plat") or ""), ini, fim, _num_i(g(l, "ped")), _num_c(g(l, "val")), _num_i(g(l, "vp")),
             _num_c(g(l, "vv")), _num_i(g(l, "cp")), _num_c(g(l, "cv")), _num_i(g(l, "cli")), agora_txt()))
        n += 1
    db.commit()
    flash(f"{n} loja(s) importada(s) para o período {ini[8:]}/{ini[5:7]} a {fim[8:]}/{fim[5:7]}.", "ok")
    return redirect(url_for("marketplaces", p=f"{ini}|{fim}"))


_MES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _mes_info(ini, fim):
    import calendar
    a, m = int(ini[:4]), int(ini[5:7])
    ult = calendar.monthrange(a, m)[1]
    return dict(mes=ini[:7], rot=f"{_MES_PT[m - 1]}/{str(a)[2:]}", parcial=not (ini[8:] == "01" and int(fim[8:]) == ult),
                ate=f"{fim[8:]}/{fim[5:7]}", dias=(datetime.strptime(fim, "%Y-%m-%d") - datetime.strptime(ini, "%Y-%m-%d")).days)


def _melhor_por_mes(periodos):
    """Só períodos dentro de um mesmo mês; se houver mais de um no mês, vale o mais longo (e o mais recente)."""
    escolhidos, fora = {}, 0
    for p in periodos:
        if p["ini"][:7] != p["fim"][:7]:
            fora += 1
            continue
        inf = _mes_info(p["ini"], p["fim"])
        atual = escolhidos.get(inf["mes"])
        if not atual or (inf["dias"], p["fim"]) > (atual[0]["dias"], atual[1]["fim"]):
            escolhidos[inf["mes"]] = (inf, p)
    return [escolhidos[k] for k in sorted(escolhidos)][-12:], fora


def mkt_mensal(db, aba):
    if aba == "lucro":
        per = db.execute("SELECT ini, fim FROM (SELECT ini, fim FROM mkt_produtos UNION SELECT ini, fim FROM mkt_variantes)").fetchall()
        esc, fora = _melhor_por_mes(per)
        meses = []
        for inf, p in esc:
            _, T, _, _ = mkt_calcular(db, p["ini"], p["fim"])
            meses.append(dict(rot=inf["rot"], parcial=inf["parcial"], ate=inf["ate"], vals=[T["receita"], T["lucro"]]))
        aviso = "O lucro depende dos custos e das taxas cadastrados." + (f" {fora} período(s) que atravessam dois meses não entram no gráfico." if fora else "")
        return dict(modo="group", series=["Faturamento", "Lucro"], cores=["#2563eb", "#059669"], meses=meses, aviso=aviso,
                    vazio="Importe um relatório de vendas por produto ou por variante de um mês para ver o gráfico.")
    per = db.execute("SELECT ini, fim FROM mkt_resumo GROUP BY ini, fim").fetchall()
    esc, fora = _melhor_por_mes(per)
    tot = {}
    dados = []
    for inf, p in esc:
        vs = {r["loja"]: r["valor_validos"] for r in db.execute("SELECT loja, valor_validos FROM mkt_resumo WHERE ini=? AND fim=?", (p["ini"], p["fim"]))}
        dados.append((inf, vs))
        for l, v in vs.items():
            tot[l] = tot.get(l, 0) + v
    lojas = sorted(tot, key=lambda l: -tot[l])
    meses = [(inf["mes"], dict(rot=inf["rot"], parcial=inf["parcial"], ate=inf["ate"], vals=[vs.get(l, 0) for l in lojas])) for inf, vs in dados]
    # meses que só têm o relatório diário (sem divisão por loja)
    tem = {m for m, _ in meses}
    extra = []
    for r in db.execute("SELECT substr(data,1,7) mes, SUM(valor_validos) v, MIN(data) a, MAX(data) b, COUNT(*) n FROM mkt_diario GROUP BY mes"):
        if r["mes"] in tem:
            continue
        a_, m_ = int(r["mes"][:4]), int(r["mes"][5:7])
        import calendar
        completo = r["a"][8:] == "01" and int(r["b"][8:]) == calendar.monthrange(a_, m_)[1] and r["n"] == calendar.monthrange(a_, m_)[1]
        extra.append((r["mes"], dict(rot=f"{_MES_PT[m_ - 1]}/{str(a_)[2:]}", parcial=not completo, ate=f"{r['b'][8:]}/{r['b'][5:7]}",
                                     vals=[0] * len(lojas) + [r["v"] or 0])))
    if extra:
        lojas = lojas + ["Total (sem divisão por loja)"]
        meses = [(m, dict(d, vals=d["vals"] + [0])) for m, d in meses] + extra
    meses = [d for _, d in sorted(meses, key=lambda x: x[0])][-12:]
    return dict(modo="stack", series=lojas, meses=meses,
                aviso=(f"{fora} período(s) que atravessam dois meses não entram no gráfico." if fora else ""),
                vazio="Importe o relatório “Vendas loja performance” de um mês para ver o gráfico.")


def mkt_ranking(rows, T):
    ordenados = sorted(rows, key=lambda g: -g["lucro"])
    top = [g for g in ordenados if g["lucro"] > 0][:10]
    prej = sorted((g for g in rows if g["lucro"] < 0), key=lambda g: g["lucro"])
    maiores = sorted(rows, key=lambda g: -g["valor"])[:15]
    baixa = [g for g in maiores if g["margem"] < T["margem"]]
    fontes = []
    for g in rows:
        for f in g["fontes"]:
            if f.get("_var"):
                fontes.append(dict(f, pai=g["produto"]))
    fontes.sort(key=lambda f: (-f["unidades"], -f["valor"]))
    return dict(top=top, prejuizo=prej, baixa=baixa, margem_media=T["margem"], variacoes=fontes[:10],
                sem_custo=sum(1 for g in rows if g["falta_custo"]))


def mkt_dia(db, dm):
    ms = [r["mes"] for r in db.execute("SELECT DISTINCT substr(data,1,7) mes FROM mkt_diario ORDER BY mes DESC")]
    if not ms:
        return dict(meses=[], dados=None)
    sel = dm if dm in ms else ms[0]
    rows = db.execute("SELECT * FROM mkt_diario WHERE substr(data,1,7)=? ORDER BY data", (sel,)).fetchall()
    meses = [dict(mes=m, rot=f"{_MES_PT[int(m[5:7]) - 1]}/{m[2:4]}") for m in ms]
    tv = sum(r["valor_validos"] for r in rows)
    pv = sum(r["validos"] for r in rows)
    melhor = max(rows, key=lambda r: r["valor_validos"])
    resumo = (f"{len(rows)} dia(s) no relatório · vendas válidas {brl_txt(tv)} · {pv} pedidos · média de {brl_txt(tv // len(rows))} por dia · "
              f"melhor dia {melhor['data'][8:]}/{melhor['data'][5:7]} ({brl_txt(melhor['valor_validos'])})")
    dados = dict(modo="stack", series=["Vendas válidas", "Canceladas"], cores=["#2563eb", "#dc2626"],
                 passoRot=1 if len(rows) <= 12 else 2, semTotal=True,
                 meses=[dict(rot=f"{r['data'][8:]}/{r['data'][5:7]}", parcial=False, ate="", vals=[r["valor_validos"], r["valor_cancelados"]]) for r in rows],
                 aviso="", vazio="")
    return dict(meses=meses, sel=sel, dados=dados, resumo=resumo)


def brl_txt(c):
    return "R$ " + f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.route("/marketplaces/dados")
def marketplaces_dados():
    db = get_db()
    resumo = []
    for r in db.execute("SELECT substr(data,1,7) mes, MIN(data) a, MAX(data) b, COUNT(*) n FROM mkt_diario GROUP BY mes ORDER BY mes DESC"):
        resumo.append(dict(tipo="Por dia", ini=r["a"], fim=r["b"], n=r["n"]))
    for tipo, tb in (("Por loja", "mkt_resumo"), ("Por produto", "mkt_produtos"), ("Por variante", "mkt_variantes")):
        for r in db.execute(f"SELECT ini, fim, COUNT(*) n FROM {tb} GROUP BY ini, fim ORDER BY fim DESC"):
            resumo.append(dict(tipo=tipo, ini=r["ini"], fim=r["fim"], n=r["n"]))
    return render_template("mkt_dados.html", titulo="Importar relatórios", resumo=resumo,
                           n_cat=db.execute("SELECT COUNT(*) FROM mkt_catalogo").fetchone()[0])


@app.route("/marketplaces/excluir", methods=["POST"])
def marketplaces_excluir():
    db = get_db()
    db.execute("DELETE FROM mkt_resumo WHERE ini=? AND fim=?", (request.form.get("ini", ""), request.form.get("fim", "")))
    db.commit()
    flash("Período excluído.", "ok")
    return redirect(url_for("marketplaces"))


@app.route("/marketplaces")
def marketplaces():
    db = get_db()
    periodos = db.execute("SELECT ini, fim FROM mkt_resumo GROUP BY ini, fim ORDER BY fim DESC, ini DESC").fetchall()
    aba = request.args.get("aba") if request.args.get("aba") in ("lucro", "ranking") else "geral"
    ctx = dict(titulo="Marketplaces", periodos=periodos, ini="", fim="", aba=aba, mensal=mkt_mensal(db, "lucro" if aba == "lucro" else "geral"), dia=None)
    if aba == "geral":
        ctx["dia"] = mkt_dia(db, request.args.get("dm", ""))
    if aba in ("lucro", "ranking"):
        lp = db.execute("""SELECT ini, fim FROM (SELECT ini, fim FROM mkt_produtos UNION SELECT ini, fim FROM mkt_variantes)
                           ORDER BY fim DESC, ini DESC""").fetchall()
        ctx["lp"] = lp
        ctx["n_cat"] = db.execute("SELECT COUNT(*) FROM mkt_catalogo").fetchone()[0]
        if lp:
            ini, fim = lp[0]["ini"], lp[0]["fim"]
            p = request.args.get("p", "")
            if "|" in p and any(p == f"{x['ini']}|{x['fim']}" for x in lp):
                ini, fim = p.split("|")
            rows, T, lojas_t, ex = mkt_calcular(db, ini, fim)
            dur = (datetime.strptime(fim, "%Y-%m-%d") - datetime.strptime(ini, "%Y-%m-%d")).days
            A = None
            for h in lp:
                if h["fim"] < ini and (datetime.strptime(h["fim"], "%Y-%m-%d") - datetime.strptime(h["ini"], "%Y-%m-%d")).days == dur:
                    _, A, _, _ = mkt_calcular(db, h["ini"], h["fim"])
                    A.update(ini=h["ini"], fim=h["fim"])
                    break
            ctx.update(ini=ini, fim=fim, prods=rows, T=T, lojas_t=lojas_t, A=A, ex=ex)
            if aba == "ranking":
                ctx["rk"] = mkt_ranking(rows, T)
    elif periodos:
        ini, fim = periodos[0]["ini"], periodos[0]["fim"]
        p = request.args.get("p", "")
        if "|" in p and any(p == f"{x['ini']}|{x['fim']}" for x in periodos):
            ini, fim = p.split("|")
        lojas = [dict(r) for r in db.execute("SELECT * FROM mkt_resumo WHERE ini=? AND fim=? ORDER BY valor_validos DESC", (ini, fim))]
        t = db.execute("""SELECT SUM(pedidos) pedidos, SUM(valor) valor, SUM(validos) validos, SUM(valor_validos) valor_validos,
            SUM(cancelados) cancelados, SUM(valor_cancelados) valor_cancelados FROM mkt_resumo WHERE ini=? AND fim=?""", (ini, fim)).fetchone()
        t = {k: (t[k] or 0) for k in t.keys()}
        for r in lojas:
            r["pct"] = r["valor_validos"] * 100.0 / t["valor_validos"] if t["valor_validos"] else 0
        historico = db.execute("""SELECT ini, fim, SUM(validos) validos, SUM(valor_validos) valor_validos, SUM(cancelados) cancelados
            FROM mkt_resumo GROUP BY ini, fim ORDER BY fim DESC, ini DESC""").fetchall()
        # período anterior de mesma duração, se existir
        dur = (datetime.strptime(fim, "%Y-%m-%d") - datetime.strptime(ini, "%Y-%m-%d")).days
        ant = None
        for h in historico:
            if h["fim"] < ini and (datetime.strptime(h["fim"], "%Y-%m-%d") - datetime.strptime(h["ini"], "%Y-%m-%d")).days == dur:
                ant = h
                break
        var = ((t["valor_validos"] - ant["valor_validos"]) * 100.0 / ant["valor_validos"]) if ant and ant["valor_validos"] else 0
        ctx.update(ini=ini, fim=fim, lojas=lojas, t=t, historico=historico, ant=ant if ant and ant["valor_validos"] else None, var=var,
                   ticket=(t["valor_validos"] // t["validos"]) if t["validos"] else 0,
                   taxa=(t["cancelados"] * 100.0 / t["pedidos"]) if t["pedidos"] else 0)
    return render_template("marketplaces.html", **ctx)


# -------------------------------- Lista de compras -----------------------------
@app.route("/compras")
def compras():
    db = get_db()
    rows = []
    for p in db.execute("SELECT * FROM produtos WHERE ativo=1 AND servico=0 AND estoque<=estoque_min ORDER BY nome"):
        meta = max(2 * (p["estoque_min"] or 0), (p["estoque_min"] or 0) + 1)
        sug = meta - (p["estoque"] or 0)
        d = dict(p)
        d["sugerida"] = round(max(sug, 1 if p["unidade"] == "un" else 0.001), 3)
        if p["unidade"] == "un":
            d["sugerida"] = int(-(-d["sugerida"] // 1))                 # unidade inteira, arredonda para cima
        rows.append(d)
    return render_template("compras.html", titulo="Lista de compras", rows=rows)


# -------------------------------- Clientes -------------------------------
@app.route("/clientes")
def clientes():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        dig = re.sub(r"\D", "", q)
        rows = db.execute("SELECT * FROM clientes WHERE nome LIKE ? OR telefone LIKE ? OR email LIKE ? "
                          "OR (?<>'' AND documento_norm LIKE ?) ORDER BY nome",
                          (f"%{q}%",) * 3 + (dig, f"%{dig}%")).fetchall()
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
        doc, doc_norm, erro_doc = ler_documento(f.get("documento"))
        if erro_doc:
            flash(erro_doc, "erro")
            return render_template("cliente_form.html", titulo="Cliente", c=f, cid=cid), 400
        if doc_norm:
            dono = db.execute("SELECT id, nome FROM clientes WHERE documento_norm=? AND id<>?",
                              (doc_norm, cid or -1)).fetchone()
            if dono:
                flash(MSG_DOC_DUP + f" (cliente: {dono['nome']})", "erro")
                return render_template("cliente_form.html", titulo="Cliente", c=f, cid=cid), 400
        dados = (nome, tel, f.get("email", "").strip(),
                 f.get("obs", "").strip(), f.get("cidade", "").strip(),
                 f.get("endereco", "").strip(), norm, doc, doc_norm)

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
                           "telefone_norm=?,documento=?,documento_norm=? WHERE id=?", dados + (cid,))
            else:
                cur = db.execute("INSERT INTO clientes(nome,telefone,email,obs,cidade,endereco,"
                                 "telefone_norm,documento,documento_norm,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 dados + (datetime.now().isoformat(" ", "seconds"),))
                cid = cur.lastrowid
            db.commit()
        except sqlite3.IntegrityError as e:      # gravação simultânea: a restrição do banco barra
            db.rollback()
            flash(MSG_DOC_DUP if "documento" in str(e) else MSG_TEL_DUP, "erro")
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
    return db.execute("SELECT id, nome, ativo, unidade FROM categorias_produto ORDER BY lower(nome)").fetchall()


MSG_CAT_DUP = "Já existe uma categoria com esse nome."
MSG_SKU_DUP = "Este SKU já está cadastrado em outro produto. Informe um código diferente."
MSG_TEL_DUP = "Este telefone já está cadastrado para outro cliente. Verifique o cadastro existente."
MSG_DOC_DUP = "Este CPF/CNPJ já está cadastrado para outro cliente. Verifique o cadastro existente."


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
        dados = request.get_json(silent=True) or {}
        nome, erro = nome_categoria(db, dados.get("nome"))
        if erro:
            return jsonify(ok=False, erro=erro), 400
        un = dados.get("unidade") if dados.get("unidade") in UNIDADES else "un"
        try:
            cur = db.execute("INSERT INTO categorias_produto(nome, unidade) VALUES(?,?)", (nome, un))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify(ok=False, erro=MSG_CAT_DUP), 400
        return jsonify(ok=True, id=cur.lastrowid, nome=nome, unidade=un)
    return jsonify(categorias=[dict(r) for r in db.execute(
        "SELECT id, nome, unidade FROM categorias_produto WHERE ativo=1 ORDER BY lower(nome)")])


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
            un = request.form.get("unidade") if request.form.get("unidade") in UNIDADES else "un"
            db.execute("INSERT INTO categorias_produto(nome, unidade) VALUES(?,?)", (nome, un))
            db.commit()
            flash("Categoria cadastrada.", "ok")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(MSG_CAT_DUP, "erro")
    return redirect(url_for("categorias_produto"))


@app.route("/produtos/categorias/<int:cid>/unidade", methods=["POST"])
def categoria_unidade(cid):
    db = get_db()
    un = request.form.get("unidade")
    if un not in UNIDADES or not db.execute("SELECT 1 FROM categorias_produto WHERE id=?", (cid,)).fetchone():
        flash("Unidade inválida.", "erro")
    else:
        db.execute("UPDATE categorias_produto SET unidade=? WHERE id=?", (un, cid))
        db.commit()
        flash("Unidade da categoria atualizada. Ela vale como padrão para novos produtos; os já cadastrados não mudam.", "ok")
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


ORDENS_PROD = ("nome", "categoria", "codigo", "preco", "minimo", "estoque")


def _txt_ord(t):
    import unicodedata
    return unicodedata.normalize("NFD", str(t or "").casefold()).encode("ascii", "ignore").decode()


def _cod_ord(t):
    """Código natural: números por valor (0002 antes de 0010), depois textos; sem código fica por último."""
    t = str(t or "").strip()
    if not t:
        return None
    return (0, int(t), "") if t.isdigit() else (1, 0, _txt_ord(t))


def ordenar_produtos(rows, ordem, desc):
    def chave(r):
        if ordem == "nome":
            return _txt_ord(r["nome"])
        if ordem == "categoria":
            return _txt_ord(r["categoria"]) or None
        if ordem == "codigo":
            return _cod_ord(r["sku"])
        if ordem == "preco":
            return r["preco"]
        if ordem == "minimo":
            return r["minimo"] or None
        return None if r["servico"] else r["estoque"]          # estoque: serviço não controla
    com = [r for r in rows if chave(r) is not None]
    sem = [r for r in rows if chave(r) is None]                 # vazios ficam sempre no fim
    desempate = lambda r: (_txt_ord(r["nome"]), r["id"])
    com.sort(key=desempate)
    com.sort(key=chave, reverse=desc)
    sem.sort(key=desempate)
    return com + sem


@app.route("/produtos")
def produtos():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        rows = db.execute("SELECT * FROM produtos WHERE ativo=1 AND (nome LIKE ? OR sku LIKE ?)",
                          (f"%{q}%",) * 2).fetchall()
    else:
        rows = db.execute("SELECT * FROM produtos WHERE ativo=1").fetchall()
    # ordenação pelos cabeçalhos da tabela (padrão: por código, do menor para o maior)
    ordem = request.args.get("o", "codigo")
    if ordem not in ORDENS_PROD:
        ordem = "codigo"
    desc = request.args.get("d") == "desc"
    rows = ordenar_produtos(rows, ordem, desc)
    por = inteiro(request.args.get("n"), 50)
    por = por if por in (50, 100, 200) else 50
    total = len(rows)
    paginas = max(1, -(-total // por))
    pg = min(max(inteiro(request.args.get("pg"), 1), 1), paginas)
    rows = rows[(pg - 1) * por: pg * por]
    # os custos NÃO são enviados nesta página: só vêm de /api/produtos/custos ao clicar em "Mostrar custos"
    return render_template("produtos.html", titulo="Produtos", rows=rows, q=q, ordem=ordem, desc=desc,
                           pg=pg, paginas=paginas, por=por, total=total,
                           de=(pg - 1) * por + 1 if total else 0, ate=min(pg * por, total))


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


@app.route("/produtos/<int:pid>/duplicar")
def produto_duplicar(pid):
    """Abre o formulário de novo produto já preenchido com os dados do original. Só o código muda
    (próximo do contador) e o estoque começa em zero; nada é gravado até clicar em Salvar."""
    db = get_db()
    p = db.execute("SELECT * FROM produtos WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    d = dict(p)
    d["sku"], d["estoque"] = None, 0
    return render_template("produto_form.html", titulo="Duplicar produto", p=d, pid=None, dup=True,
                           cats=cats_existentes(db), sku_sug=proximo_sku(db))


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
                             "prazo,obs,desconto_tipo,desconto_valor,entrega,vendedor,token,status) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'orcamento')", cab + (token,))
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


# -------------------------------- Orçamentos parados (follow-up) ---------------
MSG_FOLLOWUP_PADRAO = ("Olá, {nome}! Aqui é da {empresa}. Passando para saber se você conseguiu ver o orçamento "
                       "nº {os} ({valor}). Qualquer dúvida ou ajuste, é só me chamar! 😊")


def followup_dias(db):
    d = inteiro(cfg_get(db, "fu_dias", "3"), 3)
    return d if 1 <= d <= 90 else 3


def _followup_lista(db):
    """Orçamentos em aberto com a situação do lembrete. Devolve lista de dicts."""
    hoje = datetime.now().date()
    dias = followup_dias(db)
    res = []
    for r in db.execute("SELECT o.id, o.titulo, o.total, o.data, o.followup_em, o.followup_off, c.nome cliente, c.telefone "
                        "FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id WHERE o.status='orcamento' ORDER BY o.data"):
        d = dict(r)
        try:
            criado = datetime.strptime(r["data"][:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        ref = criado
        if r["followup_em"]:
            try:
                ref = max(ref, datetime.strptime(r["followup_em"][:10], "%Y-%m-%d").date())
            except ValueError:
                pass
        d["dias"] = (hoje - criado).days
        d["devido"] = (not r["followup_off"]) and (hoje - ref).days >= dias
        res.append(d)
    return res


def montar_msg_followup(db, o):
    modelo = cfg_get(db, "fu_msg", "") or MSG_FOLLOWUP_PADRAO
    nome = (o["cliente"] or "").strip().split(" ")[0].title() or "tudo bem"
    return (modelo.replace("{nome}", nome).replace("{empresa}", empresa_dados(db)["nome"])
            .replace("{os}", f"{o['id']:04d}").replace("{valor}", brl(o["total"])))


@app.route("/orcamentos/followup")
def followup():
    db = get_db()
    todos = _followup_lista(db)
    filtros = {"pendentes": lambda r: r["devido"], "lembrados": lambda r: bool(r["followup_em"]) and not r["followup_off"],
               "dispensados": lambda r: bool(r["followup_off"]), "todos": lambda r: True}
    aba = request.args.get("aba") if request.args.get("aba") in filtros else "pendentes"
    rows = sorted((r for r in todos if filtros[aba](r)), key=lambda r: -r["dias"])
    return render_template("followup.html", titulo="Orçamentos parados", rows=rows, aba=aba,
                           cont={k: sum(1 for r in todos if f(r)) for k, f in filtros.items()},
                           dias=followup_dias(db), modelo=cfg_get(db, "fu_msg", "") or MSG_FOLLOWUP_PADRAO,
                           numero_whatsapp=numero_whatsapp)


@app.route("/orcamentos/followup/config", methods=["POST"])
def followup_config():
    db = get_db()
    dias = inteiro(request.form.get("dias"), 0)
    msg = request.form.get("msg", "").replace("\r", "").strip()
    if not 1 <= dias <= 90:
        flash("Informe de 1 a 90 dias.", "erro")
    elif len(msg) > 800:
        flash("A mensagem passa de 800 caracteres.", "erro")
    else:
        cfg_set(db, "fu_dias", str(dias))
        cfg_set(db, "fu_msg", "" if msg == MSG_FOLLOWUP_PADRAO else msg)
        db.commit()
        flash("Configuração salva.", "ok")
    return redirect(url_for("followup"))


def _orc_followup(db, oid):
    return db.execute("SELECT o.*, c.nome cliente, c.telefone FROM orcamentos o JOIN clientes c ON c.id=o.cliente_id "
                      "WHERE o.id=? AND o.status='orcamento'", (oid,)).fetchone()


@app.route("/orcamentos/<int:oid>/followup/enviar", methods=["POST"])
def followup_enviar(oid):
    db = get_db()
    o = _orc_followup(db, oid)
    if not o:
        abort(404)
    num = numero_whatsapp(o["telefone"])
    if not num:
        flash(f"O cliente {o['cliente']} não tem um telefone válido cadastrado.", "erro")
        return redirect(url_for("followup"))
    db.execute("UPDATE orcamentos SET followup_em=? WHERE id=?", (agora_txt(), oid))
    db.commit()
    from urllib.parse import quote
    return redirect(f"https://wa.me/{num}?text={quote(montar_msg_followup(db, o))}")


@app.route("/orcamentos/<int:oid>/followup/estado", methods=["POST"])
def followup_estado(oid):
    db = get_db()
    if not _orc_followup(db, oid):
        abort(404)
    acao = request.form.get("acao")
    if acao == "lembrado":
        db.execute("UPDATE orcamentos SET followup_em=? WHERE id=?", (agora_txt(), oid))
    elif acao == "dispensar":
        db.execute("UPDATE orcamentos SET followup_off=1 WHERE id=?", (oid,))
    elif acao == "pendente":
        db.execute("UPDATE orcamentos SET followup_off=0 WHERE id=?", (oid,))
    db.commit()
    return redirect(url_for("followup", aba=request.args.get("aba") or "pendentes"))


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
                           desc_l=desconto_por_linha(db, oid, linhas, o["desconto"]),
                           comps=comps, subtotal=subtotal, pag=resumo_pagamentos(db, oid),
                           formas=FORMAS_PAG, hoje=f"{datetime.now():%Y-%m-%d}")


def desconto_por_linha(db, oid, linhas, desconto):
    """Desconto de cada item (centavos), somando exatamente o desconto do orçamento.
    Usa o rateio gravado nos componentes; orçamentos antigos (sem rateio) dividem proporcional ao valor."""
    rat = {r["linha_id"]: r["d"] for r in db.execute(
        "SELECT linha_id, SUM(desc_rateio) d FROM orcamento_itens WHERE orcamento_id=? GROUP BY linha_id", (oid,))}
    res = {l["id"]: int(rat.get(l["id"]) or 0) for l in linhas}
    if not desconto:
        return {k: 0 for k in res}
    if sum(res.values()) == desconto:
        return res
    total = sum(l["total"] for l in linhas)
    if total <= 0:
        return {k: 0 for k in res}
    res = {l["id"]: desconto * l["total"] // total for l in linhas}
    resto = desconto - sum(res.values())
    for l in sorted(linhas, key=lambda l: (-(desconto * l["total"] % total), l["id"])):
        if resto <= 0:
            break
        res[l["id"]] += 1
        resto -= 1
    return res


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
                           desc_l=desconto_por_linha(db, oid, linhas, o["desconto"]),
                           subtotal=sum(l["total"] for l in linhas), modo=modo, cabe=cabe,
                           pag=resumo_pagamentos(db, oid),
                           auto=request.args.get("auto") == "1")


FORMAS_PAG = ("Dinheiro", "Pix", "Cartão", "Transferência", "Boleto", "Outro")


def resumo_pagamentos(db, oid):
    o = db.execute("SELECT total FROM orcamentos WHERE id=?", (oid,)).fetchone()
    itens = db.execute("SELECT * FROM pagamentos WHERE orcamento_id=? ORDER BY data, id", (oid,)).fetchall()
    pago = sum(p["valor"] for p in itens)
    total = o["total"] if o else 0
    return {"itens": itens, "pago": pago, "total": total, "saldo": max(total - pago, 0)}


def sincronizar_recebimento(db, oid):
    """Venda do serviço com pagamentos: 'recebida' só quando o total foi pago (data do último pagamento)."""
    v = db.execute("SELECT id, total FROM vendas WHERE orcamento_id=? AND status='ativa'", (oid,)).fetchone()
    if not v:
        return
    r = db.execute("SELECT COALESCE(SUM(valor),0) pago, MAX(data) ultimo, COUNT(*) n FROM pagamentos "
                   "WHERE orcamento_id=?", (oid,)).fetchone()
    if not r["n"]:
        return
    db.execute("UPDATE vendas SET recebido_em=? WHERE id=?",
               (r["ultimo"] if r["pago"] >= v["total"] else None, v["id"]))


@app.route("/orcamentos/<int:oid>/pagamentos", methods=["POST"])
def pagamento_novo(oid):
    db = get_db()
    o = db.execute("SELECT status, total FROM orcamentos WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    volta = url_for("orcamento_ver", oid=oid) + "#pagamentos"
    if o["status"] not in ("aprovado", "producao", "concluido"):
        flash("Só dá para registrar pagamento de serviço aprovado.", "erro")
        return redirect(volta)
    valor = para_centavos(request.form.get("valor"))
    data = request.form.get("data", "").strip()
    forma = request.form.get("forma", "Outro")
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        flash("Informe a data do pagamento.", "erro")
        return redirect(volta)
    saldo = resumo_pagamentos(db, oid)["saldo"]
    if valor is None or valor <= 0:
        flash("Informe um valor maior que zero.", "erro")
    elif valor > saldo:
        flash(f"O valor passa do saldo a receber ({brl(saldo)}).", "erro")
    else:
        db.execute("INSERT INTO pagamentos(orcamento_id,valor,data,forma,obs,criado_em) VALUES(?,?,?,?,?,?)",
                   (oid, valor, data, forma if forma in FORMAS_PAG else "Outro",
                    request.form.get("obs", "").strip()[:120], agora_txt()))
        sincronizar_recebimento(db, oid)
        db.commit()
        flash(f"Pagamento de {brl(valor)} registrado.", "ok")
    return redirect(volta)


@app.route("/orcamentos/<int:oid>/pagamentos/<int:pid>/excluir", methods=["POST"])
def pagamento_excluir(oid, pid):
    db = get_db()
    db.execute("DELETE FROM pagamentos WHERE id=? AND orcamento_id=?", (pid, oid))
    v = db.execute("SELECT id FROM vendas WHERE orcamento_id=? AND status='ativa'", (oid,)).fetchone()
    if v:
        if db.execute("SELECT 1 FROM pagamentos WHERE orcamento_id=?", (oid,)).fetchone():
            sincronizar_recebimento(db, oid)
        else:
            db.execute("UPDATE vendas SET recebido_em=NULL WHERE id=?", (v["id"],))
    db.commit()
    flash("Pagamento excluído.", "ok")
    return redirect(url_for("orcamento_ver", oid=oid) + "#pagamentos")


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
    sincronizar_recebimento(db, oid)
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
        sincronizar_recebimento(db, oid)
        return
    db.execute("UPDATE vendas SET cliente_id=?, total=?, valor_bruto=?, desconto=?, cpv=?, "
               "cpv_pendentes=? WHERE id=?", novo + (v["id"],))
    gravar_custos(db, v["id"], itens)
    evento_venda(db, v["id"], "ajustada", v["total"], o["total"], "orçamento editado")
    sincronizar_recebimento(db, oid)


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
        "prazo,obs,desconto_tipo,desconto_valor,vendedor,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'orcamento')",
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
    pag = resumo_pagamentos(db, v["orcamento_id"]) if orc_existe else None
    return render_template("venda_ver.html", titulo=f"Venda #{vid}", v=v, itens=itens, pag=pag,
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
    if db.execute("SELECT 1 FROM vendas v JOIN pagamentos p ON p.orcamento_id=v.orcamento_id WHERE v.id=?", (vid,)).fetchone():
        flash("Esta venda tem pagamentos registrados no serviço; use a área Pagamentos do orçamento.", "erro")
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
# -------------------------------- Senha de acesso -----------------------------
_tentativas = {}


def carregar_segredo():
    """Chave das sessões guardada no banco (o login sobrevive a reinicializações) e recuperação de senha."""
    import secrets
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT)")
        r = con.execute("SELECT valor FROM config WHERE chave='app_secret'").fetchone()
        if not r:
            novo = secrets.token_hex(32)
            con.execute("INSERT INTO config(chave,valor) VALUES('app_secret',?)", (novo,))
            r = (novo,)
        app.secret_key = r[0]
        sinal = os.path.join(PASTA, "REMOVER_SENHA.txt")
        if os.path.exists(sinal):
            con.execute("DELETE FROM config WHERE chave='senha_hash'")
            try:
                os.remove(sinal)
            except OSError:
                pass
        con.commit()
    finally:
        con.close()


def _senha_hash(db):
    r = db.execute("SELECT valor FROM config WHERE chave='senha_hash'").fetchone()
    return r["valor"] if r and r["valor"] else ""


@app.before_request
def exigir_login():
    if request.endpoint in ("static", "login", None):
        return
    try:
        if _senha_hash(get_db()) and not session.get("ok"):
            return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else ""))
    except sqlite3.Error:
        return


def _proximo_seguro(p):
    return p if p and p.startswith("/") and not p.startswith("//") else url_for("dashboard")


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    h = _senha_hash(db)
    if not h:
        return redirect(url_for("dashboard"))
    prox = _proximo_seguro(request.values.get("next", ""))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        n, ate = _tentativas.get(ip, (0, 0))
        import time
        if ate > time.time():
            flash(f"Muitas tentativas. Aguarde {int(ate - time.time()) + 1} segundos.", "erro")
        elif check_password_hash(h, request.form.get("senha", "")):
            _tentativas.pop(ip, None)
            session.clear()
            session["ok"] = True
            session.permanent = bool(request.form.get("manter"))
            return redirect(prox)
        else:
            n += 1
            _tentativas[ip] = (n, time.time() + 30 if n >= 5 else 0)
            if n >= 5:
                _tentativas[ip] = (0, time.time() + 30)
            flash("Senha incorreta.", "erro")
    return render_template("login.html", proximo=prox)


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/seguranca")
def seguranca():
    return render_template("seguranca.html", titulo="Segurança", tem_senha=bool(_senha_hash(get_db())))


@app.route("/seguranca/salvar", methods=["POST"])
def seguranca_salvar():
    db = get_db()
    h = _senha_hash(db)
    nova, nova2 = request.form.get("nova", ""), request.form.get("nova2", "")
    if h and not check_password_hash(h, request.form.get("atual", "")):
        flash("A senha atual está incorreta.", "erro")
    elif len(nova) < 4:
        flash("A senha precisa ter pelo menos 4 caracteres.", "erro")
    elif nova != nova2:
        flash("As duas senhas digitadas são diferentes.", "erro")
    else:
        cfg_set(db, "senha_hash", generate_password_hash(nova)); db.commit()
        session["ok"] = True
        flash("Senha salva. A partir de agora o sistema pede a senha ao abrir.", "ok")
    return redirect(url_for("seguranca"))


@app.route("/seguranca/remover", methods=["POST"])
def seguranca_remover():
    db = get_db()
    h = _senha_hash(db)
    if h and check_password_hash(h, request.form.get("atual", "")):
        db.execute("DELETE FROM config WHERE chave='senha_hash'"); db.commit()
        flash("Senha removida.", "ok")
    else:
        flash("A senha atual está incorreta.", "erro")
    return redirect(url_for("seguranca"))


# -------------------------------- Backups -----------------------------
PASTA_BACKUPS = os.path.join(PASTA, "backups")
MANTER_BACKUPS = 30
_backup_dia = {"d": None}


def _copiar_banco(destino):
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(destino)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()


def _status_extra(ok, arquivo="", erro=""):
    """Guarda o resultado da última cópia extra (a página Backups e o Início mostram)."""
    try:
        import json
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO config(chave,valor) VALUES('backup_extra_status',?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                    (json.dumps({"ok": ok, "quando": f"{datetime.now():%Y-%m-%d %H:%M}", "arquivo": arquivo, "erro": erro}),))
        con.commit()
        con.close()
    except Exception:
        pass


def _copiar_extra(destino, nome):
    """Copia o backup para a pasta extra (se configurada), mantém só os mais recentes lá e registra o resultado."""
    try:
        con = sqlite3.connect(DB_PATH)
        r = con.execute("SELECT valor FROM config WHERE chave='backup_extra'").fetchone()
        con.close()
    except Exception:
        return
    pasta = r[0] if r and r[0] else ""
    if not pasta:
        return
    try:
        if not os.path.isdir(pasta):
            raise OSError("a pasta não foi encontrada (o Google Drive/pendrive está conectado?)")
        import shutil
        shutil.copy2(destino, os.path.join(pasta, nome))
        for pref in ("auto", "manual"):
            arqs = sorted(f for f in os.listdir(pasta) if re.fullmatch(pref + r"_\d{8}_\d{6}\.db", f))
            for velho in arqs[:-MANTER_BACKUPS]:
                try:
                    os.remove(os.path.join(pasta, velho))
                except OSError:
                    pass
        _status_extra(True, nome)
    except Exception as e:
        _status_extra(False, nome, str(e)[:200])


def estado_backup(db):
    """Situação do backup para a página Backups e o aviso do Início."""
    import json
    extra = cfg_get(db, "backup_extra", "")
    try:
        st = json.loads(cfg_get(db, "backup_extra_status", "") or "{}")
    except ValueError:
        st = {}
    antigo = False
    if st.get("ok") and st.get("quando"):
        try:
            antigo = (datetime.now() - datetime.strptime(st["quando"], "%Y-%m-%d %H:%M")).days >= 3
        except ValueError:
            pass
    aviso = ""
    if not extra and cfg_get(db, "backup_aviso_off", "") != "1":
        aviso = "sem_extra"
    elif extra and (not os.path.isdir(extra) or st.get("ok") is False):
        aviso = "falha"
    elif extra and antigo:
        aviso = "antigo"
    return dict(extra=extra, st=st, aviso=aviso, pasta_ok=bool(extra) and os.path.isdir(extra))


def fazer_backup(prefixo="auto"):
    """Copia o banco para backups/ (e para a pasta extra, se houver). Devolve o caminho ou None."""
    try:
        os.makedirs(PASTA_BACKUPS, exist_ok=True)
        nome = f"{prefixo}_{datetime.now():%Y%m%d_%H%M%S}.db"
        destino = os.path.join(PASTA_BACKUPS, nome)
        _copiar_banco(destino)
        for pref in ("auto", "manual"):                       # guarda só os mais recentes de cada tipo
            arqs = sorted(f for f in os.listdir(PASTA_BACKUPS) if f.startswith(pref + "_") and f.endswith(".db"))
            for velho in arqs[:-MANTER_BACKUPS]:
                try:
                    os.remove(os.path.join(PASTA_BACKUPS, velho))
                except OSError:
                    pass
        _copiar_extra(destino, nome)
        return destino
    except Exception:
        return None


@app.before_request
def backup_do_dia():
    hoje = f"{datetime.now():%Y%m%d}"
    if _backup_dia["d"] == hoje or request.endpoint == "static":
        return
    _backup_dia["d"] = hoje
    try:
        existe = os.path.isdir(PASTA_BACKUPS) and any(
            f.startswith(f"auto_{hoje}") for f in os.listdir(PASTA_BACKUPS))
    except OSError:
        existe = False
    if not existe:
        fazer_backup("auto")


@app.route("/backups")
def backups():
    db = get_db()
    arqs = []
    if os.path.isdir(PASTA_BACKUPS):
        for f in sorted(os.listdir(PASTA_BACKUPS), reverse=True):
            if f.endswith(".db") and f.split("_")[0] in ("auto", "manual"):
                st = os.stat(os.path.join(PASTA_BACKUPS, f))
                arqs.append({"nome": f, "kb": max(1, st.st_size // 1024),
                             "data": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M")})
    extra = cfg_get(db, "backup_extra", "")
    return render_template("backups.html", titulo="Backups", arqs=arqs, extra=extra,
                           extra_ok=bool(extra) and os.path.isdir(extra), manter=MANTER_BACKUPS,
                           est=estado_backup(db))


@app.route("/backups/agora", methods=["POST"])
def backup_agora():
    if not fazer_backup("manual"):
        flash("Não consegui fazer o backup.", "erro")
    else:
        est = estado_backup(get_db())
        if est["extra"] and est["st"].get("ok") is False:
            flash("Backup feito neste computador, mas a cópia extra falhou: " + est["st"].get("erro", ""), "erro")
        elif est["extra"]:
            flash("Backup feito e copiado também para a pasta extra.", "ok")
        else:
            flash("Backup feito (somente neste computador; configure uma pasta extra para ter uma segunda cópia).", "ok")
    return redirect(url_for("backups"))


@app.route("/backups/testar", methods=["POST"])
def backup_testar():
    pasta = cfg_get(get_db(), "backup_extra", "")
    if not pasta:
        flash("Informe e salve a pasta extra primeiro.", "erro")
    else:
        try:
            if not os.path.isdir(pasta):
                raise OSError("a pasta não foi encontrada")
            alvo = os.path.join(pasta, f"_teste_{uuid.uuid4().hex[:8]}.tmp")
            with open(alvo, "w") as fh:
                fh.write("ok")
            os.remove(alvo)
            flash("Pasta extra funcionando: consegui gravar e apagar um arquivo de teste.", "ok")
        except Exception as e:
            flash(f"Não consegui gravar na pasta extra: {e}", "erro")
    return redirect(url_for("backups"))


@app.route("/backups/dispensar-aviso", methods=["POST"])
def backup_dispensar_aviso():
    db = get_db()
    cfg_set(db, "backup_aviso_off", "1")
    db.commit()
    return redirect(url_for("dashboard"))


@app.route("/backups/config", methods=["POST"])
def backup_config():
    db = get_db()
    pasta = request.form.get("pasta", "").strip().strip('"')
    cfg_set(db, "backup_extra", pasta); db.commit()
    flash("Pasta extra salva." if pasta else "Cópia extra desativada.", "ok")
    if pasta and not os.path.isdir(pasta):
        flash("Atenção: essa pasta não existe neste computador.", "erro")
    return redirect(url_for("backups"))


@app.route("/backups/baixar/<nome>")
def backup_baixar(nome):
    if not re.fullmatch(r"(auto|manual)_\d{8}_\d{6}\.db", nome):
        abort(404)
    caminho = os.path.join(PASTA_BACKUPS, nome)
    if not os.path.isfile(caminho):
        abort(404)
    return send_file(caminho, as_attachment=True, download_name=nome)


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


def cpf_valido(d):
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        dv = sum(int(c) * p for c, p in zip(d[:n], range(n + 1, 1, -1))) * 10 % 11 % 10
        if int(d[n]) != dv:
            return False
    return True


def ler_documento(txt):
    """CPF ou CNPJ opcional. Devolve (formatado, só_dígitos, erro). Em branco = sem documento."""
    d = re.sub(r"\D", "", txt or "")
    if not d:
        return "", None, None
    if len(d) == 11 and cpf_valido(d):
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}", d, None
    if len(d) == 14 and cnpj_valido(d):
        return mascara_cnpj(d), d, None
    if len(d) not in (11, 14):
        return "", None, "CPF/CNPJ incompleto: o CPF tem 11 números e o CNPJ tem 14."
    return "", None, "CPF/CNPJ inválido: confira os números digitados."


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
.side a{color:#cbd5e1;text-decoration:none;padding:8px 12px;border-radius:8px;display:block}
.side a:hover{background:#1e293b;color:#fff}
.side a.on{background:var(--pri);color:#fff}
.side a.fim{margin-top:auto;font-size:13px}
.side .grp{margin:0}.side .grp summary{list-style:none;cursor:pointer;color:#cbd5e1;padding:8px 12px;border-radius:8px;display:flex;justify-content:space-between;align-items:center;user-select:none}
.side .grp summary::-webkit-details-marker{display:none}
.side .grp summary::after{content:"▸";font-size:12px;opacity:.7;transition:transform .15s}
.side .grp[open] summary::after{transform:rotate(90deg)}
.side .grp summary:hover{background:#1e293b;color:#fff}
.side .grp[open] summary{color:#fff;font-weight:600}
.side .grp a.sub{padding:6px 12px 6px 30px;font-size:14px;margin-left:6px;border-left:1px solid #334155;border-radius:0 8px 8px 0}
.wrap{margin-left:224px}
main{max-width:1180px;margin:24px auto;padding:0 20px}
@media(max-width:900px){
 .side .grp{width:100%}
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
{% set grupos = [
 ('🧾 Vendas', [('/clientes','👥 Clientes'),('/orcamentos','📋 Orçamentos'),('/orcamentos/followup','⏰ Orçamentos parados'),('/entregas','🚚 Entregas'),('/vendas','🛒 Vendas'),('/avaliacoes','⭐ Avaliações')]),
 ('📦 Estoque', [('/produtos','📦 Produtos'),('/compras','🛍 Lista de compras')]),
 ('💰 Dinheiro', [('/financeiro','💰 Financeiro'),('/contas','📆 Contas a pagar/receber'),('/bancos','🏦 Bancos'),('/relatorios','📈 Relatórios')]),
 ('⚙️ Gestão', [('/agenda','📅 Agenda'),('/empresa','🏢 Empresa')]) ] %}
{% macro ativo(path) %}{{ 'on' if (path == '/' and request.path == '/') or (path == '/orcamentos' and request.path.startswith('/orcamentos') and not request.path.startswith('/orcamentos/followup')) or (path != '/' and path != '/orcamentos' and request.path.startswith(path)) else '' }}{% endmacro %}
<a href="/" class="{{ ativo('/') }}">🏠 Início</a>
{% for titulo, itens in grupos %}{% set ns = namespace(on=false) %}{% for p, n in itens %}{% if request.path.startswith(p) %}{% set ns.on = true %}{% endif %}{% endfor %}
<details class="grp" {{ 'open' if ns.on else '' }}><summary>{{ titulo }}</summary>
{% for path, nome in itens %}<a href="{{ path }}" class="sub {{ ativo(path) }}">{{ nome }}</a>
{% endfor %}</details>
{% endfor %}
<a href="/marketplaces" class="{{ ativo('/marketplaces') }}">🛍 Marketplaces</a>
<a class="fim" href="{{ url_for('backup') }}">⬇ Backup</a><a href="{{ url_for('backups') }}" style="font-size:13px">🗄 Backups automáticos</a><a href="{{ url_for('seguranca') }}" style="font-size:13px">🔒 Segurança</a>{% if tem_senha %}<a href="{{ url_for('sair') }}" style="font-size:13px">↩ Sair</a>{% endif %}
</aside>
<script>
document.querySelectorAll('.side .grp').forEach(d=>d.addEventListener('toggle',()=>{
  if(d.open) document.querySelectorAll('.side .grp').forEach(o=>{ if(o!==d) o.open=false; });
}));
</script>
<div class="wrap"><main>
{% for cat, msg in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}
{% block conteudo %}{% endblock %}
</main></div></body></html>"""

DASHBOARD = r"""{% extends "base.html" %}{% block conteudo %}
<h1>Início</h1>
{% if aviso_backup == 'sem_extra' %}<div class="flash erro" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">🗄 Seus backups estão só neste computador. Se ele quebrar, você perde tudo. <a class="btn sm" href="{{ url_for('backups') }}">Configurar cópia extra</a><form method="post" action="{{ url_for('backup_dispensar_aviso') }}" style="margin:0"><button class="btn sec sm">Não mostrar mais</button></form></div>
{% elif aviso_backup == 'falha' %}<div class="flash erro">🗄 A cópia extra do backup falhou ou a pasta não foi encontrada. <a href="{{ url_for('backups') }}">Ver detalhes</a></div>
{% elif aviso_backup == 'antigo' %}<div class="flash erro">🗄 A última cópia extra do backup tem mais de 3 dias. <a href="{{ url_for('backups') }}">Ver detalhes</a></div>{% endif %}
<div class="grid">
  <div class="card kpi"><small>Orçamentos aguardando aprovação</small><b>{{ ab.n }}</b><small>{{ ab.t|brl }}</small></div>
  <div class="card kpi"><small>Orçamentos aprovados no mês</small><b>{{ ap.t|brl }}</b><small>{{ ap.n }} orçamento(s)</small></div>
  <div class="card kpi"><small>Vendas no mês</small><b>{{ v.t|brl }}</b><small>{{ v.n }} venda(s)</small></div>
  <div class="card kpi"><small>Capital investido em estoque</small><b>{{ investido|brl }}</b><small><a href="{{ url_for('produtos') }}">ver estoque</a></small></div>
  <div class="card kpi"><small>Clientes</small><b>{{ n_clientes }}</b></div>
</div>
{% for cls, txt, lnk in avisos %}<div class="flash {{ cls }}">{{ txt }} <a href="{{ lnk }}">Ver</a></div>{% endfor %}
{% if contas_ini %}<div class="card"><h2>Contas a vencer e vencidas</h2>
<p class="nota">Até 7 dias. A pagar: <b>{{ contas_tot.despesa|brl }}</b> · A receber: <b>{{ contas_tot.receita|brl }}</b></p>
<table><tr><th>Vencimento</th><th>Descrição</th><th class="n">Valor</th><th></th></tr>
{% for c in contas_ini %}<tr><td style="white-space:nowrap">{{ c.venc|dt }}<br>{% if c.dias < 0 %}<span class="tag er">{{ -c.dias }} dia(s) atrasada</span>{% elif c.dias == 0 %}<span class="tag wa">hoje</span>{% else %}<span class="tag">em {{ c.dias }} dia(s)</span>{% endif %}</td>
<td>{% if c.tipo == 'despesa' %}<span class="tag er">pagar</span>{% else %}<span class="tag ok">receber</span>{% endif %} {% if c.link %}<a href="{{ c.link }}">{{ c.descricao }}</a>{% else %}{{ c.descricao }}{% endif %}{% if c.categoria %}<br><span class="nota">{{ c.categoria }}</span>{% endif %}</td>
<td class="n">{{ c.valor|brl }}</td>
<td class="n">{% if c.origem == 'lancamento' %}<form class="inline" method="post" action="{{ url_for('conta_baixa', lid=c.id) }}"><input type="hidden" name="data" value="{{ hoje_iso }}"><input type="hidden" name="volta" value="inicio"><button class="btn sm">{{ 'Pagar' if c.tipo == 'despesa' else 'Receber' }}</button></form>{% else %}<a class="btn sec sm" href="{{ c.link }}">Registrar</a>{% endif %}</td></tr>{% endfor %}</table>
<p style="margin-top:8px"><a href="{{ url_for('contas') }}">Ver todas as contas{% if contas_mais %} (+{{ contas_mais }}){% endif %}</a></p></div>{% endif %}
{% if atrasados %}<div class="flash erro">Você tem {{ atrasados }} compromisso(s) da agenda com data vencida e ainda em aberto. <a href="{{ url_for('agenda') }}">Ver agenda</a></div>{% endif %}
<div class="card"><h2>Orçamentos aguardando aprovação</h2>
{% if abertos %}<table><tr><th>#</th><th>Cliente</th><th>Peça</th><th>Data</th><th class="n">Total</th></tr>
{% for o in abertos %}<tr><td><a href="{{ url_for('orcamento_ver', oid=o.id) }}">{{ o.id }}</a></td><td>{{ o.cliente }}</td><td>{{ o.titulo or '' }}</td><td>{{ o.data|dt }}</td><td class="n">{{ o.total|brl }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum orçamento aguardando aprovação.</p>{% endif %}
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
<div class="fin-top"><div><h1>Financeiro</h1><span class="mut" id="perLabel"></span> · <a href="{{ url_for('exportar') }}">Exportar para o contador</a></div>
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

<dialog id="dBaixa"><h2 id="bTit">Lançamento</h2><input type="hidden" id="bId">
<p class="nota" id="bInfo"></p>
<label id="bLblData">Data</label><input type="date" id="bData">
<div class="bar" style="margin:14px 0 0;justify-content:flex-end;flex-wrap:wrap"><button type="button" class="btn sec" id="bFechar">Fechar</button><button type="button" class="btn sec" id="bEd">Editar</button><button type="button" class="btn red" id="bDesf">Desfazer baixa</button><button type="button" class="btn" id="bOk">Dar baixa</button></div></dialog>

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
    h += `<tr><td>${dbr(x.data)}</td><td>${venda ? `<a href="/vendas/${x.id}">${esc(x.descricao)}</a>` : `<a href="#" data-b="${x.id}">${esc(x.descricao)}</a>`}</td><td>${esc(x.categoria)}</td><td>${tp}</td>
      <td class="n ${x.tipo === 'despesa' ? 'neg' : 'pos'}">${x.tipo === 'despesa' ? '− ' : ''}${brl(x.valor)}</td>
      <td><span class="tag ${ok ? 'ok' : 'wa'}">${SIT[x.status] || x.status}</span></td>
      <td class="ac">${venda ? '<span class="nota">automática</span>' : `${ok ? '' : `<button class="btn sm" data-p="${x.id}">${x.tipo === 'despesa' ? 'Pagar' : 'Receber'}</button> `}<button class="btn sec sm" data-e="${x.id}">Editar</button> <button class="btn red sm" data-x="${x.id}">Excluir</button>`}</td></tr>`; });
  $('lista').innerHTML = h + '</table>';
  window.LINHAS = r.linhas;
}
function baixaAbrir(x) {
  if (!x) return;
  const ok = x.status === 'pago' || x.status === 'recebido', d = x.tipo === 'despesa';
  $('bId').value = x.id;
  $('bTit').textContent = x.descricao;
  $('bInfo').innerHTML = (d ? 'Despesa' : 'Receita') + ' · ' + esc(x.categoria || '') + ' · <b>' + brl(x.valor) + '</b><br>Vencimento: ' + (dbr(x.vencimento) || '—') + ' · ' + (SIT[x.status] || x.status) + (ok && x.pagamento ? ' em ' + dbr(x.pagamento) : '');
  $('bData').value = ok && x.pagamento ? x.pagamento : new Date().toLocaleDateString('sv');
  $('bData').disabled = ok;
  $('bLblData').textContent = d ? 'Data do pagamento' : 'Data do recebimento';
  $('bOk').style.display = ok ? 'none' : ''; $('bOk').textContent = d ? 'Dar baixa (pago)' : 'Dar baixa (recebido)';
  $('bDesf').style.display = ok ? '' : 'none';
  $('bEd').onclick = () => { $('dBaixa').close(); abrir(x); };
  $('dBaixa').showModal();
}
async function baixaEnviar(corpo) {
  const r = await fetch('/api/financeiro/lancamentos/' + $('bId').value + '/baixa', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corpo)});
  const j = await r.json();
  if (!j.ok) { alert((j.erros || ['Erro']).join('\n')); return; }
  $('dBaixa').close(); atualizar();
}
$('bOk').onclick = () => baixaEnviar({data: $('bData').value});
$('bDesf').onclick = () => baixaEnviar({desfazer: true});
$('bFechar').onclick = () => $('dBaixa').close();
$('lista').addEventListener('click', async e => {
  const a = e.target.closest('a[data-b]');
  if (a) { e.preventDefault(); baixaAbrir(window.LINHAS.find(x => x.origem === 'lancamento' && x.id == a.dataset.b)); return; }
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.p) { baixaAbrir(window.LINHAS.find(x => x.origem === 'lancamento' && x.id == b.dataset.p)); }
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
<div class="card"><p>{% if c.documento %}🪪 {{ c.documento }} &nbsp; {% endif %}{% if c.telefone %}📞 {{ c.telefone }} &nbsp; {% endif %}{% if c.email %}✉ {{ c.email }}{% endif %}{% if c.cidade or c.endereco %}<br>📍 {{ c.endereco or '' }}{% if c.endereco and c.cidade %} — {% endif %}{{ c.cidade or '' }}{% endif %}</p>
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
<div class="row2"><div><label>CPF / CNPJ <span class="mut">(opcional — só se for emitir nota fiscal)</span></label><input name="documento" id="cDoc" inputmode="numeric" maxlength="18" placeholder="000.000.000-00" value="{{ (c.documento or '') if c else '' }}"></div><div></div></div>
<script>
const cdoc = document.getElementById('cDoc');
cdoc.addEventListener('input', () => { const d = cdoc.value.replace(/\D/g, '').slice(0, 14); let s = d;
  if (d.length <= 11) s = d.replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d{1,2})$/, '$1-$2');
  else s = d.replace(/^(\d{2})(\d)/, '$1.$2').replace(/^(\d{2})\.(\d{3})(\d)/, '$1.$2.$3').replace(/\.(\d{3})(\d)/, '.$1/$2').replace(/(\d{4})(\d)/, '$1-$2');
  cdoc.value = s; });
</script>
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
<div class="bar"><form><input name="q" value="{{ q }}" placeholder="Buscar por nome ou código"><input type="hidden" name="o" value="{{ ordem }}">{% if desc %}<input type="hidden" name="d" value="desc">{% endif %}{% if por != 50 %}<input type="hidden" name="n" value="{{ por }}">{% endif %}<button class="btn sec">Buscar</button></form>
<a class="btn" href="{{ url_for('produto_form') }}">+ Novo produto</a></div>
<div class="card">{% if rows %}{% macro th(chave, rotulo, cls='') %}<th{{ ' class="' ~ cls ~ '"' if cls else '' }}><a href="{{ url_for('produtos', q=q or None, o=chave, d=('desc' if (ordem == chave and not desc) else None), n=(por if por != 50 else None)) }}" title="Ordenar por {{ rotulo|lower }}" style="color:inherit;text-decoration:none;white-space:nowrap">{{ rotulo }}{% if ordem == chave %} {{ '▼' if desc else '▲' }}{% endif %}</a></th>{% endmacro %}
<table><tr>{{ th('nome','Produto') }}{{ th('categoria','Categoria') }}{{ th('codigo','Código') }}<th class="n">Custo</th>{{ th('preco','Preço de venda','n') }}{{ th('minimo','Mínimo','n') }}{{ th('estoque','Estoque','n') }}<th class="n">Investido</th><th></th></tr>
{% for p in rows %}<tr><td>{{ p.nome }}{% if p.servico %} <span class="tag">serviço</span>{% endif %}</td><td>{{ p.categoria or '' }}</td><td>{{ p.sku or '' }}</td>
<td class="n cst" data-id="{{ p.id }}" data-k="custo">••••••</td>
<td class="n">{{ p.preco|brl }}{% if p.unidade != 'un' %} <span class="mut">/ {{ p.unidade|ul }}</span>{% endif %}</td>
<td class="n">{% if p.minimo %}{{ p.minimo|brl }}{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n">{% if not p.servico %}<span class="tag {{ 'er' if p.estoque <= 0 else ('wa' if p.estoque <= p.estoque_min else 'ok') }}">{{ p.estoque|num(4) }} {{ p.unidade|ul }}</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n">{% if not p.servico %}<span class="cst" data-id="{{ p.id }}" data-k="investido">••••••</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n" style="white-space:nowrap"><a class="btn sec sm" href="{{ url_for('produto_form', pid=p.id) }}">Editar</a>
<a class="btn sec sm" href="{{ url_for('produto_duplicar', pid=p.id) }}" title="Criar um novo produto com os mesmos dados">Duplicar</a>
{% if not p.servico %}<a class="btn sec sm" href="{{ url_for('produto_movimentos', pid=p.id) }}">Histórico</a>{% endif %}
<form class="inline" method="post" action="{{ url_for('produto_excluir', pid=p.id) }}" onsubmit="return confirm('Excluir este produto?')"><button class="btn red sm">Excluir</button></form></td></tr>{% endfor %}</table>
<div class="bar noprint" style="margin:12px 0 0;justify-content:space-between;align-items:center">
<span class="mut">Mostrando {{ de }}–{{ ate }} de {{ total }} produto(s)</span>
<div class="bar" style="margin:0;align-items:center">
{% if pg > 1 %}<a class="btn sec sm" href="{{ url_for('produtos', q=q or None, o=ordem, d=('desc' if desc else None), n=(por if por != 50 else None), pg=(pg - 1 if pg > 2 else None)) }}">← Anterior</a>{% endif %}
<span class="mut">Página {{ pg }} de {{ paginas }}</span>
{% if pg < paginas %}<a class="btn sec sm" href="{{ url_for('produtos', q=q or None, o=ordem, d=('desc' if desc else None), n=(por if por != 50 else None), pg=pg + 1) }}">Próxima →</a>{% endif %}
<span class="mut" style="margin-left:8px">Por página:</span>{% for k in (50, 100, 200) %}<a class="btn {{ '' if k == por else 'sec' }} sm" href="{{ url_for('produtos', q=q or None, o=ordem, d=('desc' if desc else None), n=(k if k != 50 else None)) }}">{{ k }}</a>{% endfor %}</div></div>
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
<h1>{{ 'Editar produto' if pid else ('Duplicar produto' if dup else 'Novo produto') }}</h1>
{% if dup %}<p class="mut" style="margin:-6px 0 10px">Os dados do produto original foram copiados. O código vem com o próximo da sequência e o estoque começa em zero. Nada é salvo até você clicar em Salvar.</p>{% endif %}
<form class="card" method="post"{% if dup %} action="{{ url_for('produto_form') }}"{% endif %}>
<div class="row2"><div><label>Nome *</label><input name="nome" value="{{ p.nome if p else '' }}" required autofocus></div>
<div><label>Código / SKU</label><input name="sku" value="{{ sku_sug if dup else ((p.sku or '') if p else (sku_sug or '')) }}">{% if not pid %}<small class="mut">Sugestão automática (contador). Troque por um código seu, se quiser; em branco, o sistema usa o próximo número.</small>{% endif %}</div></div>
<div class="row2"><div><label>Categoria (como aparece nos acompanhamentos do orçamento)</label>
{% set sel = (p.categoria_id|string) if p and p.categoria_id else '' %}
<div style="display:flex;gap:6px"><select name="categoria_id" id="catSel"><option value="">— selecionar categoria —</option>{% for c in cats %}{% if c.ativo or c.id|string == sel %}<option value="{{ c.id }}" data-un="{{ c.unidade or 'un' }}"{{ ' selected' if c.id|string == sel else '' }}>{{ c.nome }}{{ '' if c.ativo else ' (inativa)' }}</option>{% endif %}{% endfor %}</select>
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
  const o = new Option(j.nome, j.id, true, true), s = document.getElementById('catSel'); o.dataset.un = j.unidade || 'un'; s.add(o); s.value = j.id;
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
document.getElementById('catSel').addEventListener('change', function () {      // unidade padrão da categoria
  const o = this.options[this.selectedIndex], u = o && o.dataset.un;
  if (u && ['un', 'm2', 'ml'].includes(u)) { un.value = u; tog(); }
});
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
  <div id="avBox" style="display:none"><div class="av-h">Componentes avulsos</div><div class="ct av"><div id="avulsos"></div></div>
<p class="nota" style="margin:6px 0 0"><b>Como cobrar:</b> <b>por unidade</b> = preço × quantidade (ex.: mão de obra, fixação); <b>por m²</b> = preço × largura × altura da peça (com o arredondamento de 5 cm); <b>por ML</b> = preço × perímetro da peça + perda de moldura. A quantidade calculada pode ser trocada à mão.</p></div>
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
function novoCustom(){return novoComp({key:'x:'+(++nCustom),ativo:true,custom:true,unidade:'un',qtd:'1'});}
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
function listaHTML(i){const g=CAT[i];return '<datalist id="dl_'+i+'">'+g.produtos.map(p=>'<option value="'+esc(p.rot)+'">'+esc(brl(p.preco)+(p.un!=='un'?' / '+UL[p.un]:'')+(p.controla?' · estoque '+fq(p.estoque):''))+'</option>').join('')+'</datalist>';}
/* a lista de materiais de cada categoria só é montada quando você clica no campo (leve mesmo com milhares de produtos) */
function listas(){ $('dls').innerHTML=''; }
document.addEventListener('focusin',e=>{const l=e.target.getAttribute&&e.target.getAttribute('list');
  if(l&&/^dl_\d+$/.test(l)&&!document.getElementById(l)&&CAT[+l.slice(3)]) $('dls').insertAdjacentHTML('beforeend',listaHTML(+l.slice(3)));});
function linhaHTML(r){
  const idx=CAT.findIndex(g=>g.nome===r.categoria);
  let mat;
  if(!r.custom) mat='<input class="mat" list="dl_'+idx+'" placeholder="Selecione o material..." autocomplete="off">';
  else if(r.produto_id) mat='<div class="fixo mat-fixo"></div>';
  else mat='<div class="comb"><input class="cnome" placeholder="Descrição (ex.: Mão de obra)" maxlength="120"><select class="cun" style="max-width:150px"><option value="">Escolha como cobrar…</option><option value="un">Por unidade (× quantidade)</option><option value="m2">Por m² (largura × altura)</option><option value="ml">Por ML (perímetro + perda)</option></select></div>';
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
function duplicar(i){
  const it=linhas[i]; if(!it) return;
  linhas.splice(i+1,0,JSON.parse(JSON.stringify(it)));
  abertos.clear(); if(editIdx>i) editIdx++;
  sujo(); editar(i+1);
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
      let r='<tr class="'+(i===editIdx?'editando':'')+'"><td>'+(i+1)+'</td><td><a href="#" class="arrow" data-det="'+i+'" title="Ver composição">'+(ab?'▾':'▸')+'</a><b>'+esc(v.nome)+'</b>'+(v.descricao?'<div class="sm mut" style="margin-left:16px">'+esc(v.descricao)+'</div>':'')+'</td><td>'+esc(v.medidas||'—')+'</td><td>'+comp+'</td><td class="n"><input type="number" class="qi" min="1" step="1" value="'+v.qtd+'" data-q="'+i+'" aria-label="Quantidade"></td><td class="n">'+esc(v.valor_unit_txt)+'</td><td class="n"><b>'+esc(v.total_txt)+'</b></td><td class="n" style="white-space:nowrap"><button type="button" class="btn sec sm" data-ed="'+i+'">Editar</button> <button type="button" class="btn sec sm" data-dup="'+i+'" title="Duplicar este item e abrir na edição">Duplicar</button> <button type="button" class="btn red sm" data-del="'+i+'">Excluir</button></td></tr>';
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
  else if(t.matches('[data-dup]')) duplicar(parseInt(t.dataset.dup,10));
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
{% if o.status in ('aprovado', 'producao', 'concluido') %}<form class="inline" method="post" action="{{ url_for('pronto_enviar', oid=o.id) }}" target="_blank"><button class="btn" style="background:#16a34a">💬 {{ 'Avisar de novo: pronto' if o.pronto_enviado_em else 'Avisar que está pronto' }}</button></form>{% endif %}
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
<td class="n">{{ l.qtd }}</td><td class="n">{{ l.valor_unit|brl }}</td><td class="n">{% if desc_l[l.id] %}<span class="mut" style="text-decoration:line-through">{{ l.total|brl }}</span><br><span style="color:#dc2626;font-size:12px">desconto − {{ desc_l[l.id]|brl }}</span><br><b>{{ (l.total - desc_l[l.id])|brl }}</b>{% else %}{{ l.total|brl }}{% endif %}</td></tr>{% endfor %}</table>
<div class="total">{% if o.desconto %}<span class="mut" style="font-size:15px">Subtotal {{ subtotal|brl }} · Desconto{% if o.desconto_tipo == 'pct' %} ({{ o.desconto_valor|num(2) }}%){% endif %} − {{ o.desconto|brl }}</span><br>{% endif %}Total: <b>{{ o.total|brl }}</b></div>
{% if o.obs %}<p><b>Observações:</b> {{ o.obs }}</p>{% endif %}
{% if emp.obs_inst or emp.rodape %}<div class="rodape">{% if emp.obs_inst %}<div>{{ emp.obs_inst }}</div>{% endif %}{% if emp.rodape %}<div>{{ emp.rodape }}</div>{% endif %}</div>{% endif %}
</div>

{% if o.status in ('aprovado', 'producao', 'concluido') or pag.itens %}
<div class="card noprint" id="pagamentos"><h2>Pagamentos do serviço <span class="tag {{ 'ok' if pag.saldo == 0 and pag.itens else ('wa' if pag.pago else '') }}">{% if pag.saldo == 0 and pag.itens %}quitado{% elif pag.pago %}saldo {{ pag.saldo|brl }}{% else %}nada recebido{% endif %}</span></h2>
<p class="mut">Registre o <b>sinal</b> na aprovação e os demais pagamentos até a entrega. A receita continua sendo contada na conclusão; aqui controlamos o dinheiro que já entrou.</p>
{% if pag.itens %}<table><tr><th>Data</th><th>Forma</th><th>Obs.</th><th class="n">Valor</th><th></th></tr>
{% for p in pag.itens %}<tr><td>{{ p.data|dia }}</td><td>{{ p.forma }}</td><td>{{ p.obs or '' }}</td><td class="n">{{ p.valor|brl }}</td>
<td class="n"><form class="inline" method="post" action="{{ url_for('pagamento_excluir', oid=o.id, pid=p.id) }}" onsubmit="return confirm('Excluir este pagamento?')"><button class="btn red sm">Excluir</button></form></td></tr>{% endfor %}</table>{% endif %}
<p style="margin:10px 0">Total do serviço <b>{{ o.total|brl }}</b> · Recebido <b>{{ pag.pago|brl }}</b> · <b>Saldo a receber {{ pag.saldo|brl }}</b></p>
{% if pag.saldo > 0 and o.status in ('aprovado', 'producao', 'concluido') %}
<form method="post" action="{{ url_for('pagamento_novo', oid=o.id) }}" class="bar" style="align-items:end;flex-wrap:wrap">
<div><label>Valor (R$)</label><input name="valor" id="pgValor" inputmode="decimal" placeholder="0,00" style="width:130px" required></div>
<div><label>Data</label><input type="date" name="data" value="{{ hoje }}" style="width:160px" required></div>
<div><label>Forma</label><select name="forma" style="width:150px">{% for f in formas %}<option>{{ f }}</option>{% endfor %}</select></div>
<div style="flex:1;min-width:160px"><label>Obs.</label><input name="obs" maxlength="120" placeholder="{{ 'ex.: sinal' if not pag.itens else '' }}"></div>
<button class="btn sm">Registrar pagamento</button>
<button class="btn sec sm" type="button" onclick="document.getElementById('pgValor').value='{{ ('%.2f'|format(pag.saldo / 100)).replace('.', ',') }}'">Preencher saldo</button></form>{% endif %}</div>
{% endif %}
<div class="card noprint"><h2>Situação do orçamento</h2>
<p class="mut">Ao <b>aprovar</b>, os produtos usados são descontados do estoque. Se o orçamento for <b>cancelado</b>, voltar para <b>orçamento</b> ou <b>em aberto</b>, editado ou excluído, o estoque é ajustado automaticamente.</p><div class="bar">
{% for k, v in ORC.items() %}{% if k != o.status %}<form class="inline" method="post" action="{{ url_for('orcamento_status', oid=o.id) }}"><input type="hidden" name="status" value="{{ k }}"><button class="btn {{ 'red' if k == 'recusado' else ('sec' if k in ('orcamento', 'aberto') else '') }} sm">Marcar como: {{ v[0] }}</button></form>{% endif %}{% endfor %}
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
<div class="card"><form method="post" action="{{ url_for('categoria_nova') }}" class="bar" style="align-items:end"><div style="flex:1;min-width:220px"><label>Nova categoria</label><input name="nome" maxlength="60" placeholder="Ex.: Ferragens"></div><div><label>Unidade padrão</label><select name="unidade"><option value="un" {{ 'selected' if "un" == 'un' else '' }}>Unitário (un)</option><option value="m2" {{ 'selected' if "un" == 'm2' else '' }}>m²</option><option value="ml" {{ 'selected' if "un" == 'ml' else '' }}>Metro linear (ML)</option></select></div><button class="btn">Cadastrar</button></form></div>
<div class="card"><table><tr><th>Nome</th><th>Unidade padrão</th><th>Produtos</th><th>Situação</th><th></th></tr>
{% for c in rows %}<tr><td><form method="post" action="{{ url_for('categoria_renomear', cid=c.id) }}" class="bar" style="margin:0;flex-wrap:nowrap"><input name="nome" value="{{ c.nome }}" maxlength="60" style="min-width:160px"><button class="btn sec sm">Renomear</button></form></td>
<td><form method="post" action="{{ url_for('categoria_unidade', cid=c.id) }}" style="margin:0"><select name="unidade" onchange="this.form.submit()" style="width:auto"><option value="un" {{ 'selected' if c.unidade == 'un' else '' }}>Unitário (un)</option><option value="m2" {{ 'selected' if c.unidade == 'm2' else '' }}>m²</option><option value="ml" {{ 'selected' if c.unidade == 'ml' else '' }}>Metro linear (ML)</option></select></form></td>
<td>{{ c.usos }}</td><td><span class="tag {{ 'ok' if c.ativo else '' }}">{{ 'ativa' if c.ativo else 'inativa' }}</span></td>
<td class="n"><form class="inline" method="post" action="{{ url_for('categoria_ativar', cid=c.id) }}"><button class="btn sec sm">{{ 'Desativar' if c.ativo else 'Ativar' }}</button></form>
{% if c.usos %}<form class="inline" method="post" action="{{ url_for('categoria_reclassificar', cid=c.id) }}" onsubmit="return confirm('Mover todos os produtos desta categoria para a categoria escolhida?')"><select name="destino" style="width:auto"><option value="">mover produtos para…</option>{% for d in rows if d.id != c.id and d.ativo %}<option value="{{ d.id }}">{{ d.nome }}</option>{% endfor %}</select> <button class="btn sec sm">Reclassificar</button></form>
{% else %}<form class="inline" method="post" action="{{ url_for('categoria_excluir', cid=c.id) }}" onsubmit="return confirm('Excluir esta categoria?')"><button class="btn red sm">Excluir</button></form>{% endif %}</td></tr>{% endfor %}</table>
<p class="hint">Renomear atualiza os produtos da categoria. Categoria em uso não pode ser excluída (reclassifique antes). Desativar só a tira da lista de novos cadastros. A unidade padrão (un, m² ou ML) é preenchida sozinha ao escolher a categoria num produto novo; os produtos já cadastrados não mudam.</p></div>
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
<td class="m">{% if l.largura and l.altura %}{{ l.largura|num }} × {{ l.altura|num }} cm{% endif %}</td><td class="v">{{ l.valor_unit|brl }}</td><td class="v">{% if desc_l[l.id] %}<span style="text-decoration:line-through">{{ l.total|brl }}</span><div class="sm">desconto − {{ desc_l[l.id]|brl }}</div><b>{{ (l.total - desc_l[l.id])|brl }}</b>{% else %}{{ l.total|brl }}{% endif %}</td></tr>{% endfor %}</tbody></table>
<div class="tot">{% if o.desconto %}<div>Subtotal: {{ subtotal|brl }}</div><div>Desconto{% if o.desconto_tipo == 'pct' %} ({{ o.desconto_valor|num(2) }}%){% endif %}: − {{ o.desconto|brl }}</div>{% endif %}<div class="gt">Total: {{ o.total|brl }}</div>{% if pag and pag.itens %}<div>Já pago: {{ pag.pago|brl }}</div><div><b>Saldo a pagar: {{ pag.saldo|brl }}</b></div>{% endif %}</div>
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
<div class="bar"><button type="button" onclick="window.print()">Imprimir</button><button type="button" id="btnImg" style="background:#16a34a" onclick="copiarImagem(this)">📋 Copiar como imagem</button><span id="msgImg" style="font-size:13px"></span><a class="s" href="{{ url_for('orcamento_ver', oid=o.id) }}">Voltar ao orçamento</a></div>
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
<script>
async function copiarImagem(btn){
  const msg=document.getElementById('msgImg'), rot=btn.textContent;
  const alvo=document.querySelector('.via1')||document.querySelector('.folha');
  const nome='orcamento-{{ "%04d"|format(o.id) }}.png';
  btn.disabled=true; btn.textContent='Gerando imagem...'; msg.textContent='';
  try{
    const larg=alvo.offsetWidth+24, clone=alvo.cloneNode(true);
    for(const im of clone.querySelectorAll('img')){
      const b=await (await fetch(im.src)).blob();
      im.src=await new Promise(r=>{const f=new FileReader(); f.onload=()=>r(f.result); f.readAsDataURL(b);});
    }
    const css=Array.from(document.querySelectorAll('style')).map(s=>s.textContent).join('\n').replace(/&/g,'&amp;').replace(/</g,'&lt;');
    const html=new XMLSerializer().serializeToString(clone);
    const base='<div xmlns="http://www.w3.org/1999/xhtml" style="width:'+larg+'px;padding:12px;box-sizing:border-box;background:#fff;color:#000;font:10pt/1.3 Arial,Helvetica,sans-serif"><style>'+css+' .folha{width:auto!important;margin:0!important;padding:0!important;box-shadow:none!important}</style>';
    // mede a altura real renderizando fora da tela
    const med=document.createElement('div'); med.style.cssText='position:fixed;left:-99999px;top:0'; med.innerHTML=base+clone.outerHTML+'</div>'; document.body.appendChild(med);
    const alt=med.firstChild.offsetHeight; document.body.removeChild(med);
    const svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+larg+'" height="'+alt+'"><foreignObject width="100%" height="100%">'+base+html+'</div></foreignObject></svg>';
    const img=new Image(); img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
    await img.decode();
    const k=2, cv=document.createElement('canvas'); cv.width=larg*k; cv.height=alt*k;
    const cx=cv.getContext('2d'); cx.fillStyle='#fff'; cx.fillRect(0,0,cv.width,cv.height); cx.drawImage(img,0,0,cv.width,cv.height);
    const blob=await new Promise(r=>cv.toBlob(r,'image/png'));
    if(!blob) throw new Error('sem imagem');
    try{
      await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
      msg.style.color='#15803d'; msg.textContent='✓ Copiado! Cole no WhatsApp com Ctrl+V.';
    }catch(e){
      const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=nome; document.body.appendChild(a); a.click(); a.remove();
      msg.style.color='#b45309'; msg.textContent='Não consegui copiar direto; baixei a imagem ('+nome+'). Anexe no WhatsApp.';
    }
  }catch(e){
    msg.style.color='#b91c1c'; msg.textContent='Não foi possível gerar a imagem: '+e;
  }finally{btn.disabled=false; btn.textContent=rot;}
}
</script>
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
<div class="card" id="comparativo"><h2>Conferência: vendas × entradas do banco — {{ ano }}</h2>
<p class="nota">Compara o que o sistema registra como <b>recebido de vendas</b> com tudo que <b>entrou nos bancos</b>. As entradas do banco incluem também Pix avulsos e repasses de marketplace, então a diferença não precisa ser zero: serve para ver se algum mês tem dinheiro entrando sem venda registrada (diferença positiva) ou venda marcada como recebida sem entrada no banco (negativa).</p>
{% if comparativo %}<table><tr><th>Mês</th><th class="n">Vendas concluídas</th><th class="n">Recebido (sistema)</th><th class="n">A receber</th><th class="n">Entradas nos bancos</th><th class="n">Diferença (banco − recebido)</th></tr>
{% for c in comparativo %}<tr><td>{{ meses[c.mes - 1] }}</td><td class="n">{{ c.vendas|brl }}</td><td class="n">{{ c.recebido|brl }}</td><td class="n">{{ c.areceber|brl }}</td>
<td class="n">{% if c.tem_banco %}{{ c.banco|brl }}{% else %}<span class="mut">sem extrato</span>{% endif %}</td>
<td class="n">{% if c.tem_banco %}<span class="{{ 'c' if c.dif >= 0 else 'd' }}">{{ c.dif|brl }}</span>{% else %}<span class="mut">—</span>{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Sem dados neste ano.</p>{% endif %}</div>
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
{% if pag and pag.itens %}<p>Recebido pelos pagamentos do serviço: <b>{{ pag.pago|brl }}</b> de {{ v.total|brl }}{% if pag.saldo %} · <b>saldo {{ pag.saldo|brl }}</b>{% endif %}. <a href="{{ url_for('orcamento_ver', oid=v.orcamento_id) }}#pagamentos">Ver / registrar pagamentos</a></p>
{% elif v.recebido_em %}<p>Recebido em <b>{{ v.recebido_em|dia }}</b>. <span class="mut">A receita desta venda já foi contada na conclusão; registrar o recebimento não soma de novo.</span></p>
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

BACKUPS = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><div><h1>Backups</h1><span class="mut">O sistema salva uma cópia do banco de dados sozinho, uma vez por dia, na pasta <b>backups</b> (ao lado do gestao.db). Ficam guardadas as últimas {{ manter }}.</span></div>
<form method="post" action="{{ url_for('backup_agora') }}"><button class="btn">Fazer backup agora</button></form></div>
<div class="card"><h2>Cópia extra (opcional)</h2>
<form method="post" action="{{ url_for('backup_config') }}" class="nv" style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
<div style="flex:1;min-width:280px"><label>Pasta extra (ex.: pasta do Google Drive, OneDrive ou pendrive)</label><input name="pasta" value="{{ extra }}" placeholder="C:\Users\voce\Google Drive\Backups CIR" style="width:100%"></div><button class="btn">Salvar</button></form>
{% if extra %}<p class="nota">Cada backup também é copiado para essa pasta (ficam as últimas {{ manter }} de cada tipo). {% if extra_ok %}<span class="tag ok">pasta encontrada</span>{% else %}<span class="tag er">pasta não encontrada</span>{% endif %}</p>
{% if est.st.quando %}<p class="nota">Última cópia extra: {% if est.st.ok %}<span class="tag ok">ok</span> {{ est.st.quando[8:10] }}/{{ est.st.quando[5:7] }}/{{ est.st.quando[:4] }} {{ est.st.quando[11:] }} ({{ est.st.arquivo }}){% else %}<span class="tag er">falhou</span> {{ est.st.erro }}{% endif %}</p>{% else %}<p class="nota">Ainda não houve cópia extra. Clique em “Fazer backup agora” para testar.</p>{% endif %}
<form method="post" action="{{ url_for('backup_testar') }}"><button class="btn sec sm">Testar a pasta</button></form>{% endif %}</div>
<div class="card"><h2>Backups guardados</h2>{% if arqs %}<table><tr><th>Arquivo</th><th>Data</th><th class="n">Tamanho</th><th></th></tr>
{% for a in arqs %}<tr><td>{{ a.nome }}</td><td>{{ a.data }}</td><td class="n">{{ a.kb }} KB</td><td class="n"><a class="btn sec sm" href="{{ url_for('backup_baixar', nome=a.nome) }}">Baixar</a></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum backup ainda.</p>{% endif %}</div>
<div class="card"><h2>Como restaurar</h2><p class="nota">Feche o sistema, renomeie o <b>gestao.db</b> atual para <b>gestao_antigo.db</b>, copie o backup desejado para a pasta do sistema e renomeie para <b>gestao.db</b>. Abra o sistema de novo.</p></div>
{% endblock %}
"""

CONTAS = r"""{% extends "base.html" %}{% block head %}<style>
.ab{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.ab a{padding:5px 12px;border-radius:999px;border:1px solid var(--bd);background:#fff;color:inherit;text-decoration:none;font-size:13.5px}
.ab a.on{background:#eff6ff;border-color:#bfdbfe;color:var(--pri);font-weight:600}
.kp{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}.kp .card{margin:0;padding:12px 16px}.kp small{color:var(--mut);display:block}.kp b{font-size:20px;font-variant-numeric:tabular-nums}
.acoes{display:flex;gap:6px;justify-content:flex-end;align-items:center;flex-wrap:wrap}.acoes form{margin:0;display:flex;gap:6px;align-items:center}.acoes input[type=date]{width:auto;padding:4px 6px}
.fnv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;align-items:end}
.c{color:#15803d}.d{color:#b91c1c}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Contas a pagar e a receber</h1><span class="mut">Tudo que ainda vai sair ou entrar, por vencimento. Ao dar baixa, o lançamento vira pago/recebido no Financeiro.</span></div></div>
<div class="kp"><div class="card"><small>Vencidas</small><b class="d">{{ tot.vencidas|brl }}</b><small>{{ tot.n_vencidas }} conta(s)</small></div>
<div class="card"><small>Vencem em 7 dias</small><b>{{ tot.semana|brl }}</b><small>{{ tot.n_semana }} conta(s)</small></div>
<div class="card"><small>Total em aberto</small><b>{{ tot.aberto|brl }}</b><small>nesta lista</small></div></div>
<div class="ab">{% for k, r in (('pagar','A pagar'),('receber','A receber'),('pagas','Pagas / recebidas')) %}<a class="{{ 'on' if aba == k else '' }}" href="{{ url_for('contas', aba=k) }}">{{ r }}</a>{% endfor %}
{% if aba != 'pagas' %}<span style="flex:1"></span>{% for k, r in (('todas','Todas'),('vencidas','Vencidas'),('semana','Próx. 7 dias'),('mes','Este mês')) %}<a class="{{ 'on' if filtro == k else '' }}" href="{{ url_for('contas', aba=aba, filtro=k) }}">{{ r }}</a>{% endfor %}{% endif %}</div>
<div class="card">{% if rows %}<table><tr><th>Vence</th><th>Descrição</th><th>Categoria</th><th class="n">Valor</th><th>Situação</th><th></th></tr>
{% for r in rows %}<tr><td>{{ (r.venc or '')|dia }}</td>
<td>{% if r.link %}<a href="{{ r.link }}">{{ r.descricao }}</a>{% else %}{{ r.descricao }}{% endif %}</td><td>{{ r.categoria or '' }}</td>
<td class="n {{ 'c' if r.tipo == 'receita' else 'd' }}">{{ r.valor|brl }}</td>
<td>{% if r.pago %}<span class="tag ok">{{ 'pago' if r.tipo == 'despesa' else 'recebido' }} {{ r.pago|dia }}</span>
{% elif r.dias is none %}<span class="tag">sem vencimento</span>{% elif r.dias < 0 %}<span class="tag er">vencida há {{ -r.dias }} dia(s)</span>{% elif r.dias == 0 %}<span class="tag wa">vence hoje</span>{% else %}<span class="tag">em {{ r.dias }} dia(s)</span>{% endif %}</td>
<td><div class="acoes">
{% if r.origem == 'lancamento' %}{% if r.pago %}<form method="post" action="{{ url_for('conta_desfazer', lid=r.id) }}"><button class="btn ghost sm">Desfazer</button></form>
{% else %}<form method="post" action="{{ url_for('conta_baixa', lid=r.id) }}"><input type="date" name="data" value="{{ hoje }}"><button class="btn sm">{{ 'Pagar' if r.tipo == 'despesa' else 'Receber' }}</button></form>
<form method="post" action="{{ url_for('conta_excluir', lid=r.id) }}" onsubmit="return confirm('Excluir esta conta?')"><button class="btn red sm">Excluir</button></form>{% endif %}
{% else %}<a class="btn sm" href="{{ r.link }}">Registrar recebimento</a>{% endif %}</div></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nada por aqui.</p>{% endif %}</div>
<div class="card"><h2>Nova conta</h2>
<form method="post" action="{{ url_for('conta_nova') }}"><div class="fnv">
<div><label>Tipo</label><select name="tipo" id="cTipo"><option value="despesa">A pagar (despesa)</option><option value="receita">A receber (receita)</option></select></div>
<div style="grid-column:span 2"><label>Descrição</label><input name="descricao" maxlength="120" required placeholder="ex.: Aluguel, boleto fornecedor"></div>
<div><label>Categoria</label><select name="categoria_id" id="cCat" required>{% for c in cats %}<option value="{{ c.id }}" data-tipo="{{ c.tipo }}">{{ c.nome }}</option>{% endfor %}</select></div>
<div><label>Valor (R$)</label><input name="valor" inputmode="decimal" required placeholder="0,00"></div>
<div><label>1º vencimento</label><input type="date" name="vencimento" value="{{ hoje }}" required></div>
<div><label>Parcelas (mensais)</label><input type="number" name="parcelas" value="1" min="1" max="60"></div>
<div><button class="btn">Adicionar</button></div></div>
<p class="nota">Com mais de 1 parcela, o sistema cria uma conta por mês (mesmo dia), com o valor informado em cada uma.</p></form></div>
<script>
const t = document.getElementById('cTipo'), c = document.getElementById('cCat');
function filtra() { let primeira = null; for (const o of c.options) { const ok = o.dataset.tipo === t.value; o.hidden = !ok; o.disabled = !ok; if (ok && !primeira) primeira = o; }
  if (c.selectedOptions[0] && c.selectedOptions[0].disabled && primeira) c.value = primeira.value; }
t.addEventListener('change', filtra); filtra();
</script>
{% endblock %}
"""

ENTREGAS = r"""{% extends "base.html" %}{% block head %}<style>
.acoes{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}.acoes form{margin:0}.acoes .btn{white-space:nowrap}
.wpp{background:#16a34a;color:#fff}.wpp:hover{background:#15803d}
textarea.msg{width:100%;min-height:130px;font:inherit}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Entregas</h1><span class="mut">Serviços aprovados ou em produção, por data de entrega. A data é definida no orçamento (campo “Entrega prevista”).</span></div></div>
<div class="card">{% if rows %}<table><tr><th>Entrega</th><th>Serviço</th><th>Cliente</th><th>Situação</th><th class="n">Saldo</th><th></th></tr>
{% for r in rows %}<tr><td>{% if r.entrega %}{{ r.entrega|dia }}{% else %}<span class="mut">sem data</span>{% endif %}</td>
<td><a href="{{ url_for('orcamento_ver', oid=r.id) }}">#{{ r.id }}</a> <span class="mut">{{ r.titulo or '' }}</span></td>
<td>{{ r.cliente }}<br><span class="mut" style="font-size:12.5px">{{ r.telefone or 'sem telefone' }}</span></td>
<td>{% if r.dias is none %}<span class="tag">sem data</span>{% elif r.dias < 0 %}<span class="tag er">atrasado {{ -r.dias }} dia(s)</span>{% elif r.dias == 0 %}<span class="tag wa">entrega hoje</span>{% elif r.dias == 1 %}<span class="tag wa">amanhã</span>{% else %}<span class="tag">em {{ r.dias }} dias</span>{% endif %}
 <span class="tag {{ 'ok' if r.status == 'concluido' else '' }}">{{ ORC[r.status][0] }}</span>{% if r.pronto_enviado_em %} <span class="tag ok" title="Aviso de pronto enviado">avisado {{ r.pronto_enviado_em|dt }}</span>{% endif %}</td>
<td class="n">{{ r.saldo|brl }}</td>
<td><div class="acoes">
{% if r.num %}<form method="post" action="{{ url_for('pronto_enviar', oid=r.id) }}" target="_blank"><button class="btn sm wpp">💬 {{ 'Avisar de novo' if r.pronto_enviado_em else 'Avisar que está pronto' }}</button></form>{% endif %}
{% if r.status != 'concluido' %}<form method="post" action="{{ url_for('orcamento_status', oid=r.id) }}"><input type="hidden" name="status" value="concluido"><button class="btn sec sm" title="Marca como concluído / entregue">Concluir</button></form>{% endif %}
</div></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Nenhum serviço em andamento.</p>{% endif %}</div>
<div class="card"><h2>Mensagem “pronto para retirada”</h2>
<form method="post" action="{{ url_for('pronto_config') }}">
<textarea class="msg" name="msg" maxlength="600">{{ modelo }}</textarea>
<p class="nota">Use <b>{nome}</b>, <b>{empresa}</b>, <b>{os}</b> e <b>{saldo}</b> (vira “Saldo a pagar na retirada: R$ …” ou “Pagamento já quitado.”).</p>
<button class="btn">Salvar mensagem</button></form></div>
{% endblock %}
"""

RELATORIOS = r"""{% extends "base.html" %}{% block head %}<style>
.fl{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:14px}.fl select,.fl input{width:auto}.fl label{margin:0 0 3px}
.c{color:#15803d}.d{color:#b91c1c}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Relatórios</h1><span class="mut">O que vende mais, o que dá mais lucro e quem não volta há tempo. Só conta venda concluída.</span></div><a class="btn sec" href="{{ url_for('exportar') }}">📤 Exportar para o contador</a></div>
<form method="get" class="fl"><div><label>Mês</label><select name="mes"><option value="0">Ano todo</option>{% for m in meses %}<option value="{{ loop.index }}"{{ ' selected' if loop.index == mes else '' }}>{{ m }}</option>{% endfor %}</select></div>
<div><label>Ano</label><input type="number" name="ano" value="{{ ano }}" min="2000" max="2100" style="width:96px"></div>
<div><label>Cliente inativo após (dias)</label><input type="number" name="dias" value="{{ dias }}" min="15" max="1000" style="width:110px"></div><button class="btn sec">Atualizar</button></form>
<div class="card"><h2>Produtos e materiais mais vendidos</h2>
{% if prods %}<table><tr><th>Produto</th><th class="n">Quantidade</th><th class="n">Receita</th><th class="n">Custo</th><th class="n">Margem</th><th class="n">Margem %</th></tr>
{% for p in prods %}<tr><td>{{ p.nome }}</td><td class="n">{{ p.qtd|num(2) }}</td><td class="n">{{ p.receita|brl }}</td>
<td class="n">{% if p.custo is not none %}{{ p.custo|brl }}{% else %}<span class="mut">sem custo</span>{% endif %}</td>
<td class="n">{% if p.custo is not none %}<span class="{{ 'c' if p.margem >= 0 else 'd' }}">{{ p.margem|brl }}</span>{% else %}—{% endif %}</td>
<td class="n">{% if p.custo is not none and p.receita %}{{ ('%.0f'|format(p.margem * 100 / p.receita)) }}%{% else %}—{% endif %}</td></tr>{% endfor %}</table>
<p class="nota">Receita = preço dos componentes no orçamento, antes de descontos. Custo = preço de custo cadastrado na hora da venda. “Sem custo” = produto sem custo cadastrado (não é contado como zero).</p>
{% else %}<p class="mut">Sem vendas no período.</p>{% endif %}</div>
<div class="row2"><div class="card"><h2>Melhores clientes</h2>
{% if clientes %}<table><tr><th>Cliente</th><th class="n">Compras</th><th class="n">Total</th><th class="n">Ticket médio</th></tr>
{% for c in clientes %}<tr><td><a href="{{ url_for('cliente_ver', cid=c.id) }}">{{ c.nome }}</a></td><td class="n">{{ c.n }}</td><td class="n">{{ c.total|brl }}</td><td class="n">{{ (c.total // c.n)|brl }}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">Sem vendas no período.</p>{% endif %}</div>
<div class="card"><h2>Clientes que não voltam há {{ dias }}+ dias</h2>
{% if inativos %}<table><tr><th>Cliente</th><th>Última compra</th><th class="n">Total já comprado</th><th></th></tr>
{% for c in inativos %}<tr><td><a href="{{ url_for('cliente_ver', cid=c.id) }}">{{ c.nome }}</a><br><span class="mut" style="font-size:12.5px">{{ c.telefone or 'sem telefone' }}</span></td><td>{{ c.ultima|dia }} <span class="mut">({{ c.dias_sem }} dias)</span></td><td class="n">{{ c.total|brl }}</td>
<td class="n">{% if c.wa %}<a class="btn sm" style="background:#16a34a" target="_blank" href="{{ c.wa }}">💬 Chamar</a>{% endif %}</td></tr>{% endfor %}</table>
<p class="nota">O botão abre o WhatsApp com uma mensagem de reaproximação; nada é enviado sem você apertar enviar.</p>
{% else %}<p class="mut">Nenhum cliente nessa situação.</p>{% endif %}</div></div>
{% endblock %}
"""

COMPRAS = r"""{% extends "base.html" %}{% block head %}<style>
td input[type=number]{width:100px;text-align:right}
@media print{.noprint{display:none!important}}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Lista de compras</h1><span class="mut">Produtos com estoque no mínimo, abaixo dele ou negativo. A quantidade sugerida leva o estoque a <b>2× o mínimo</b> (ou cobre o que está faltando); ajuste como quiser.</span></div>
<div class="bar noprint"><button class="btn sec" id="bCopiar">Copiar lista</button><a class="btn" id="bWpp" target="_blank" style="background:#16a34a" href="#">💬 Enviar por WhatsApp</a><button class="btn sec" onclick="window.print()">🖨 Imprimir</button></div></div>
{% if rows %}<div class="card"><table id="tb"><tr><th class="noprint"></th><th>Produto</th><th>Código</th><th class="n">Estoque</th><th class="n">Mínimo</th><th class="n">Comprar</th><th>Un.</th><th class="n">Custo unit.</th><th class="n">Estimado</th></tr>
{% for p in rows %}<tr data-nome="{{ p.nome }}" data-un="{{ p.unidade|ul }}" data-custo="{{ p.custo or 0 }}">
<td class="noprint"><input type="checkbox" class="ck" checked></td><td>{{ p.nome }}</td><td>{{ p.sku or '' }}</td>
<td class="n {{ 'd' if p.estoque < 0 else '' }}" {% if p.estoque < 0 %}style="color:#b91c1c"{% endif %}>{{ p.estoque|num(3) }}</td><td class="n">{{ p.estoque_min|num(3) }}</td>
<td class="n"><input type="number" class="qt" step="any" min="0" value="{{ p.sugerida|num(3) }}"></td><td>{{ p.unidade|ul }}</td>
<td class="n">{{ p.custo|brl if p.custo else '—' }}</td><td class="n est">—</td></tr>{% endfor %}
<tr><td class="noprint"></td><td colspan="7" class="n"><b>Total estimado</b></td><td class="n"><b id="tot">—</b></td></tr></table>
<p class="nota">O estimado usa o custo cadastrado de cada produto; produto sem custo fica fora da soma.</p></div>
{% else %}<div class="card"><p class="mut">Nenhum produto precisa de reposição agora. 👍</p></div>{% endif %}
<script>
const brl = c => 'R$ ' + (c / 100).toFixed(2).replace('.', ',').replace(/\B(?=(\d{3})+(?!\d))/g, '.');
const fmt = n => String(Math.round(n * 1000) / 1000).replace('.', ',');
function recalcula() {
  let tot = 0;
  document.querySelectorAll('#tb tr[data-nome]').forEach(tr => {
    const q = parseFloat(tr.querySelector('.qt').value) || 0, c = parseInt(tr.dataset.custo) || 0;
    const sub = Math.round(q * c); tr.querySelector('.est').textContent = c ? brl(sub) : '—';
    if (tr.querySelector('.ck').checked) tot += sub;
  });
  document.getElementById('tot') && (document.getElementById('tot').textContent = brl(tot));
  const linhas = [];
  document.querySelectorAll('#tb tr[data-nome]').forEach(tr => {
    const q = parseFloat(tr.querySelector('.qt').value) || 0;
    if (tr.querySelector('.ck').checked && q > 0) linhas.push('- ' + tr.dataset.nome + ': ' + fmt(q) + ' ' + tr.dataset.un);
  });
  window.TEXTO = 'Lista de compras - {{ empresa }}\n' + linhas.join('\n');
  document.getElementById('bWpp') && (document.getElementById('bWpp').href = 'https://wa.me/?text=' + encodeURIComponent(window.TEXTO));
}
document.querySelectorAll('#tb .qt, #tb .ck').forEach(e => e.addEventListener('input', recalcula));
document.getElementById('bCopiar') && document.getElementById('bCopiar').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(window.TEXTO); alert('Lista copiada.'); } catch (e) { prompt('Copie a lista:', window.TEXTO); } });
recalcula();
</script>
{% endblock %}
"""

EXPORTAR = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><div><h1>Exportar para o contador</h1><span class="mut">Gera um arquivo .zip com planilhas (CSV) que abrem direto no Excel: vendas, financeiro, movimento dos bancos, pagamentos de serviços e um resumo.</span></div></div>
<div class="card"><form method="get" action="{{ url_for('exportar_baixar') }}" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
<div><label>Mês</label><select name="mes" style="width:auto"><option value="0">Ano todo</option>{% for m in meses %}<option value="{{ loop.index }}"{{ ' selected' if loop.index == mes else '' }}>{{ m }}</option>{% endfor %}</select></div>
<div><label>Ano</label><input type="number" name="ano" value="{{ ano }}" min="2000" max="2100" style="width:96px"></div>
<button class="btn">⬇ Baixar .zip</button></form>
<p class="nota" style="margin-top:12px">Dentro do .zip: <b>vendas.csv</b>, <b>financeiro.csv</b> (receitas e despesas lançadas), <b>bancos.csv</b> (extratos importados, separados do financeiro), <b>pagamentos_servicos.csv</b> e <b>resumo.csv</b> (DRE do período). Valores em reais com vírgula; separador de colunas “;”.</p></div>
{% endblock %}
"""

LOGIN = r"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar — {{ empresa }}</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f4f6;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#0f172a}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:28px;width:min(380px,92vw);box-shadow:0 10px 30px #0001}
h1{font-size:20px;margin:0 0 4px}p{color:#64748b;margin:0 0 16px;font-size:14px}label{display:block;font-size:13px;color:#64748b;margin:10px 0 4px}
input[type=password]{width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;font-size:16px}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:16px;cursor:pointer}
.erro{background:#fee2e2;color:#7f1d1d;padding:9px 12px;border-radius:8px;font-size:14px;margin-bottom:10px}.chk{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:13.5px;color:#475569}</style></head>
<body><form class="box" method="post"><h1>{{ empresa }}</h1><p>Digite a senha para entrar no sistema.</p>
{% for cat, msg in get_flashed_messages(with_categories=true) %}<div class="erro">{{ msg }}</div>{% endfor %}
<input type="hidden" name="next" value="{{ proximo }}"><label>Senha</label><input type="password" name="senha" autofocus autocomplete="current-password" required>
<label class="chk"><input type="checkbox" name="manter" value="1"> Manter conectado neste computador por 30 dias</label>
<button>Entrar</button></form></body></html>
"""

SEGURANCA = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><div><h1>Segurança</h1><span class="mut">Senha opcional para abrir o sistema. Sem senha, qualquer pessoa que abra o endereço neste computador vê tudo.</span></div></div>
<div class="card"><h2>Senha de acesso: {% if tem_senha %}<span class="tag ok">ativada</span>{% else %}<span class="tag">desativada</span>{% endif %}</h2>
<form method="post" action="{{ url_for('seguranca_salvar') }}" style="max-width:420px">
{% if tem_senha %}<label>Senha atual</label><input type="password" name="atual" autocomplete="current-password" required>{% endif %}
<label>{{ 'Nova senha' if tem_senha else 'Criar senha' }} (mínimo 4 caracteres)</label><input type="password" name="nova" autocomplete="new-password" minlength="4" required>
<label>Repita a senha</label><input type="password" name="nova2" autocomplete="new-password" minlength="4" required>
<div class="bar" style="margin-top:12px"><button class="btn">{{ 'Trocar senha' if tem_senha else 'Ativar senha' }}</button></div></form></div>
{% if tem_senha %}<div class="card"><h2>Desativar a senha</h2><form method="post" action="{{ url_for('seguranca_remover') }}" style="max-width:420px">
<label>Senha atual</label><input type="password" name="atual" required><div class="bar" style="margin-top:12px"><button class="btn red">Remover senha</button></div></form></div>
<div class="card"><h2>Esqueci a senha</h2><p class="nota">Feche o sistema, crie um arquivo de texto vazio chamado <b>REMOVER_SENHA.txt</b> na mesma pasta do <b>gestao.db</b> e abra o sistema de novo: a senha é removida (e o arquivo apagado). Seus dados não são afetados.</p></div>{% endif %}
{% endblock %}
"""

MARKETPLACES_HTML = r"""{% extends "base.html" %}{% block head %}<style>
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}
.cards .card{margin:0}.cards b{display:block;font-size:1.4em;margin-top:4px}
.barra{background:var(--borda,#e5e7eb);border-radius:6px;height:10px;min-width:90px}.barra i{display:block;height:10px;border-radius:6px;background:#2563eb}
</style>{% endblock %}{% block conteudo %}
<div class="cab"><div><h1>Marketplaces</h1><span class="mut">Overview das vendas por loja e lucro por produto, importados dos relatórios do UpSeller (“Vendas loja performance” e “Vendas por Produtos”). Fica separado do Financeiro e do DRE para não duplicar receita.</span></div></div>
<div class="bar" style="margin:0 0 12px;justify-content:space-between"><div class="bar" style="margin:0"><a class="btn {{ '' if aba=='geral' else 'sec' }}" href="/marketplaces">Visão geral</a><a class="btn {{ '' if aba=='lucro' else 'sec' }}" href="/marketplaces?aba=lucro">Produtos e lucro</a><a class="btn {{ '' if aba=='ranking' else 'sec' }}" href="/marketplaces?aba=ranking">Ranking</a></div><a class="btn sec" href="/marketplaces/dados">⬆ Importar relatórios e catálogo</a></div>
{% if aba == 'lucro' %}{% include "mkt_lucro.html" %}{% elif aba == 'ranking' %}{% include "mkt_ranking.html" %}{% elif not periodos and not (dia and dia.meses) %}<div class="card"><p class="mut">Nenhum relatório importado ainda.</p></div>{% else %}
<div class="card"><h2>{{ 'Faturamento e lucro por mês' if aba == 'lucro' else 'Vendas válidas por mês' }}</h2><div id="gLeg" class="bar" style="margin:0 0 6px;gap:14px"></div><div id="gMes" style="position:relative"></div>
<p class="nota" id="gNota"></p><details><summary class="nota">Ver como tabela</summary><div id="gTab" style="overflow:auto"></div></details></div>
{% if dia and dia.dados %}<div class="card"><div class="bar" style="margin:0 0 6px;justify-content:space-between"><h2 style="margin:0">Vendas por dia</h2>
<form method="get" class="bar" style="margin:0"><select name="dm" onchange="this.form.submit()">{% for m in dia.meses %}<option value="{{ m.mes }}" {% if m.mes == dia.sel %}selected{% endif %}>{{ m.rot }}</option>{% endfor %}</select></form></div>
<p class="nota" style="margin:0 0 6px">{{ dia.resumo }}</p><div id="gDiaLeg" class="bar" style="margin:0 0 6px;gap:14px"></div><div id="gDia" style="position:relative"></div>
<p class="nota" id="gDiaNota"></p><details><summary class="nota">Ver como tabela</summary><div id="gDiaTab" style="overflow:auto"></div></details></div>{% endif %}
{% if periodos %}
<div class="card bar"><form method="get" class="bar" style="margin:0"><label style="margin:0">Período</label><select name="p" onchange="this.form.submit()">
{% for p in periodos %}<option value="{{ p.ini }}|{{ p.fim }}" {% if p.ini == ini and p.fim == fim %}selected{% endif %}>{{ p.ini|dia }} a {{ p.fim|dia }}</option>{% endfor %}</select></form>
<form method="post" action="/marketplaces/excluir" style="margin:0" onsubmit="return confirm('Excluir os números deste período?')"><input type="hidden" name="ini" value="{{ ini }}"><input type="hidden" name="fim" value="{{ fim }}"><button class="btn red sm">Excluir período</button></form></div>
<div class="cards">
<div class="card">Vendas válidas<b>{{ t.valor_validos|brl }}</b>{% if ant %}<span class="nota">{{ '%+.1f'|format(var) }}% vs. período anterior{% endif %}</span></div>
<div class="card">Pedidos válidos<b>{{ t.validos }}</b></div>
<div class="card">Ticket médio<b>{{ ticket|brl }}</b></div>
<div class="card">Cancelamentos<b>{{ t.cancelados }}</b><span class="nota">{{ t.valor_cancelados|brl }} · {{ '%.1f'|format(taxa) }}% dos pedidos</span></div>
</div>
<div class="card"><h2>Por loja</h2><table><tr><th>Loja</th><th>Plataforma</th><th class="n">Pedidos válidos</th><th class="n">Vendas válidas</th><th class="n">Ticket médio</th><th class="n">Cancelados</th><th class="n">Cancel. (R$)</th><th>Participação</th></tr>
{% for r in lojas %}<tr><td>{{ r.loja }}</td><td>{{ r.plataforma }}</td><td class="n">{{ r.validos }}</td><td class="n">{{ r.valor_validos|brl }}</td><td class="n">{{ (r.valor_validos // r.validos if r.validos else 0)|brl }}</td><td class="n">{{ r.cancelados }}</td><td class="n">{{ r.valor_cancelados|brl }}</td>
<td><div class="barra"><i style="width:{{ r.pct }}%"></i></div><span class="nota">{{ '%.1f'|format(r.pct) }}%</span></td></tr>{% endfor %}
<tr><td colspan="2"><b>Total</b></td><td class="n"><b>{{ t.validos }}</b></td><td class="n"><b>{{ t.valor_validos|brl }}</b></td><td class="n"><b>{{ ticket|brl }}</b></td><td class="n"><b>{{ t.cancelados }}</b></td><td class="n"><b>{{ t.valor_cancelados|brl }}</b></td><td></td></tr></table>
<p class="nota">“Válidas” exclui pedidos cancelados. Total bruto do período (com cancelados): {{ t.valor|brl }} em {{ t.pedidos }} pedidos.</p></div>
<div class="card"><h2>Histórico de períodos importados</h2><table><tr><th>Período</th><th class="n">Pedidos válidos</th><th class="n">Vendas válidas</th><th class="n">Cancelados</th></tr>
{% for h in historico %}<tr><td><a href="/marketplaces?p={{ h.ini }}|{{ h.fim }}">{{ h.ini|dia }} a {{ h.fim|dia }}</a></td><td class="n">{{ h.validos }}</td><td class="n">{{ h.valor_validos|brl }}</td><td class="n">{{ h.cancelados }}</td></tr>{% endfor %}</table>
<p class="nota">Períodos diferentes não são somados entre si, pois podem se sobrepor.</p></div>
{% endif %}{% endif %}
<script type="application/json" id="dMes">{{ mensal|tojson }}</script>
{% if dia and dia.dados %}<script type="application/json" id="dDia">{{ dia.dados|tojson }}</script>{% endif %}
<script>
function desenha(D, el, leg, nota, tab) {
  if (!el) return;
  const brl = c => 'R$ ' + (c / 100).toFixed(2).replace('.', ',').replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  const curto = c => { const v = c / 100; return Math.abs(v) >= 1000 ? 'R$ ' + (v / 1000).toFixed(v % 1000 ? 1 : 0).replace('.', ',') + ' mil' : 'R$ ' + Math.round(v); };
  if (!D.meses.length) { el.innerHTML = '<p class="mut">' + D.vazio + '</p>'; return; }
  const cores = ['#2563eb', '#f59e0b', '#059669', '#7c3aed', '#dc2626', '#0891b2'];
  const S = D.series.map((n, i) => ({n, c: D.cores ? D.cores[i] : cores[i % cores.length]}));
  leg.innerHTML = S.length > 1 ? S.map(s => '<span style="display:inline-flex;align-items:center;gap:6px"><i style="width:10px;height:10px;border-radius:3px;background:' + s.c + '"></i>' + s.n + '</span>').join('') : '';
  const W = 720, H = 270, L = 70, R = 12, T = 12, B = 34, pw = W - L - R, ph = H - T - B, M = D.meses, n = M.length;
  const tot = m => D.modo === 'stack' ? m.vals.reduce((a, b) => a + Math.max(b, 0), 0) : Math.max(...m.vals, 0);
  let mx = Math.max(...M.map(tot), 1); const pas = Math.pow(10, Math.floor(Math.log10(mx / 4))); const passo = Math.ceil(mx / 4 / pas) * pas; mx = passo * 4;
  const mn = D.modo === 'stack' ? 0 : Math.min(0, ...M.flatMap(m => m.vals)); const lo = mn < 0 ? -Math.ceil(-mn / passo) * passo : 0; const span = mx - lo;
  const y = v => T + ph - (v - lo) / span * ph, bw = Math.min(64, pw / n * 0.6);
  let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;max-height:320px" role="img" aria-label="Gráfico por mês">';
  for (let v = lo; v <= mx + 1; v += passo) s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="currentColor" stroke-opacity=".15"/><text x="' + (L - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end" font-size="11" fill="currentColor" fill-opacity=".7">' + curto(v) + '</text>';
  M.forEach((m, k) => {
    const cx = L + pw / n * (k + .5); let g = '<g class="gb" data-k="' + k + '">';
    if (D.modo === 'stack') { let acc = 0; m.vals.forEach((v, i) => { if (v <= 0) return; const h = v / span * ph; g += '<rect x="' + (cx - bw / 2) + '" y="' + (y(acc + v) + 1) + '" width="' + bw + '" height="' + Math.max(h - 2, 1) + '" rx="3" fill="' + S[i].c + '"/>'; acc += v; }); }
    else { const w = bw / m.vals.length; m.vals.forEach((v, i) => { const y0 = y(0), y1 = y(v); g += '<rect x="' + (cx - bw / 2 + i * w + 1) + '" y="' + Math.min(y0, y1) + '" width="' + (w - 2) + '" height="' + Math.max(Math.abs(y1 - y0), 1) + '" rx="3" fill="' + S[i].c + '"/>'; }); }
    g += '<rect x="' + (cx - pw / n / 2) + '" y="' + T + '" width="' + pw / n + '" height="' + ph + '" fill="transparent"/></g>';
    s += g + ((k % (D.passoRot || 1)) ? '' : '<text x="' + cx + '" y="' + (H - 12) + '" text-anchor="middle" font-size="12" fill="currentColor" fill-opacity=".8">' + m.rot + (m.parcial ? '*' : '') + '</text>');
  });
  el.innerHTML = s + '</svg><div class="gTip" style="position:absolute;display:none;pointer-events:none;background:#111827;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.5;z-index:5;white-space:nowrap"></div>';
  const tip = el.querySelector('.gTip');
  el.querySelectorAll('.gb').forEach(g => {
    const m = M[g.dataset.k];
    g.addEventListener('mousemove', e => {
      const r = el.getBoundingClientRect();
      tip.innerHTML = '<b>' + m.rot + (m.parcial ? ' (parcial até ' + m.ate + ')' : '') + '</b><br>' + S.map((x, i) => '<span style="color:' + x.c + '">●</span> ' + x.n + ': ' + brl(m.vals[i])).join('<br>') + (D.modo === 'stack' && S.length > 1 ? '<br><b>Total: ' + brl(tot(m)) + '</b>' : '');
      tip.style.display = 'block'; tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 190) + 'px'; tip.style.top = (e.clientY - r.top + 14) + 'px';
    });
    g.addEventListener('mouseleave', () => tip.style.display = 'none');
  });
  nota.textContent = M.some(m => m.parcial) ? '* mês parcial: o relatório importado não cobre o mês inteiro. ' + (D.aviso || '') : (D.aviso || '');
  tab.innerHTML = '<table><tr><th>Mês</th>' + S.map(s => '<th class="n">' + s.n + '</th>').join('') + '</tr>' + M.map(m => '<tr><td>' + m.rot + (m.parcial ? '*' : '') + '</td>' + m.vals.map(v => '<td class="n">' + brl(v) + '</td>').join('') + '</tr>').join('') + '</table>';
}
desenha(JSON.parse(document.getElementById('dMes').textContent), document.getElementById('gMes'), document.getElementById('gLeg'), document.getElementById('gNota'), document.getElementById('gTab'));
(function () { const d = document.getElementById('dDia'); if (d) desenha(JSON.parse(d.textContent), document.getElementById('gDia'), document.getElementById('gDiaLeg'), document.getElementById('gDiaNota'), document.getElementById('gDiaTab')); })();
</script>{% endblock %}"""

MKT_LUCRO_HTML = r"""{% if not lp %}<div class="card"><p class="mut">Nenhum relatório “Vendas por Produtos” importado ainda. No UpSeller exporte o relatório de vendas por produtos e importe acima.</p></div>{% else %}
<div class="card bar" style="margin:0 0 12px"><form method="get" class="bar" style="margin:0"><input type="hidden" name="aba" value="lucro"><label style="margin:0">Período</label><select name="p" onchange="this.form.submit()">
{% for p in lp %}<option value="{{ p.ini }}|{{ p.fim }}" {% if p.ini == ini and p.fim == fim %}selected{% endif %}>{{ p.ini|dia }} a {{ p.fim|dia }}</option>{% endfor %}</select></form>
<form method="post" action="/marketplaces/excluir-produtos" style="margin:0" onsubmit="return confirm('Excluir os produtos deste período? Custos e taxas cadastrados continuam salvos.')"><input type="hidden" name="ini" value="{{ ini }}"><input type="hidden" name="fim" value="{{ fim }}"><button class="btn red sm">Excluir período</button></form></div>
<div class="card"><h2>{{ 'Faturamento e lucro por mês' if aba == 'lucro' else 'Vendas válidas por mês' }}</h2><div id="gLeg" class="bar" style="margin:0 0 6px;gap:14px"></div><div id="gMes" style="position:relative"></div>
<p class="nota" id="gNota"></p><details><summary class="nota">Ver como tabela</summary><div id="gTab" style="overflow:auto"></div></details></div>
<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
{% macro variacao(a,b) %}{% if b %}{% set v = (a - b) * 100.0 / b %}<span class="nota" style="color:{{ '#15803d' if v >= 0 else '#b91c1c' }}">{{ '▲' if v >= 0 else '▼' }} {{ '%.1f'|format(v|abs) }}%</span>{% else %}<span class="nota">&nbsp;</span>{% endif %}{% endmacro %}
<div class="card">Faturamento{{ variacao(T.receita, A.receita if A else 0) }}<b>{{ T.receita|brl }}</b></div>
<div class="card">Preço de custo{{ variacao(T.custo, A.custo if A else 0) }}<b>{{ T.custo|brl }}</b></div>
<div class="card">Comissão da plataforma{{ variacao(T.comissao, A.comissao if A else 0) }}<b>{{ T.comissao|brl }}</b></div>
<div class="card">Impostos{{ variacao(T.imposto, A.imposto if A else 0) }}<b>{{ T.imposto|brl }}</b></div>
<div class="card">Lucro{{ variacao(T.lucro, A.lucro if A else 0) }}<b style="color:{{ '#15803d' if T.lucro >= 0 else '#b91c1c' }}">{{ T.lucro|brl }}</b></div>
<div class="card">Margem<b>{{ '%.1f'|format(T.margem) }}%</b><span class="nota">lucro ÷ faturamento</span></div>
<div class="card">Markup<b>{{ '%.1f'|format(T.markup) if T.custo else '—' }}{{ '%' if T.custo else '' }}</b><span class="nota">lucro ÷ custo</span></div>
</div>
<p class="nota">Fonte dos dados deste período: {{ 'relatório de vendas por variante (cada variação com custo próprio)' if T.fonte_var else 'relatório de vendas por produto' }}.</p>
{% if T.sem_custo %}<div class="flash erro" style="margin-bottom:12px">{{ T.sem_custo }} produto(s) sem custo cadastrado — o lucro está <b>superestimado</b> até você preencher o custo abaixo.</div>{% endif %}
{% if T.n_abaixo %}<div class="flash erro" style="margin-bottom:12px">⚠ {{ T.n_abaixo }} produto(s) estão sendo vendidos <b>abaixo do preço mínimo</b> para a margem de {{ T.margem_alvo }}%. Veja a coluna “Preço mín.” em vermelho.</div>{% endif %}
{% if A %}<p class="nota">Comparação com o período anterior ({{ A.ini|dia }} a {{ A.fim|dia }}), usando os mesmos custos e taxas de hoje.</p>{% endif %}
<div class="card"><h2>Margem desejada</h2><p class="nota">Preço mínimo = custo ÷ (1 − comissão − imposto − margem). É o menor preço de venda que ainda deixa essa margem de lucro, loja por loja.</p>
<label style="margin:0">Margem desejada sobre o preço de venda (%)</label> <input id="margemAlvo" value="{{ T.margem_alvo }}" inputmode="decimal" style="width:80px;text-align:right"></div>
<div class="card"><h2>Taxas por loja (%)</h2><p class="nota">Digite a comissão da plataforma e o imposto de cada loja; salva ao sair do campo. Os valores se aplicam sobre o faturamento de cada produto.</p>
<table><tr><th>Loja</th><th class="n">Comissão da plataforma (%)</th><th class="n">Imposto (%)</th><th class="n">Faturamento</th></tr>
{% for l in lojas_t %}<tr><td>{{ l.loja }}</td><td class="n"><input class="tx" data-loja="{{ l.loja }}" data-campo="comissao" value="{{ l.comissao }}" inputmode="decimal" style="width:80px;text-align:right"></td>
<td class="n"><input class="tx" data-loja="{{ l.loja }}" data-campo="imposto" value="{{ l.imposto }}" inputmode="decimal" style="width:80px;text-align:right"></td><td class="n">{{ l.receita|brl }}</td></tr>{% endfor %}</table></div>
{% if ex.sem_vinc and ex.opcoes %}<div class="card"><h2>Sem vínculo com o catálogo ({{ ex.sem_vinc|length }})</h2>
<p class="nota">Estas vendas não bateram com nenhum produto do catálogo (nem por ID do anúncio, nem por SKU). Escolha a qual produto padrão cada uma pertence; a escolha fica salva para os próximos relatórios.</p>
<div class="bar" style="margin:0 0 10px"><button class="btn sm" id="bSug">Aceitar todas as sugestões</button></div>
<table><tr><th>Venda</th><th>Loja</th><th>SKU / ID</th><th class="n">Faturamento</th><th>Produto padrão</th></tr>
{% for f in ex.sem_vinc %}<tr><td style="max-width:300px">{{ f.produto }}</td><td>{{ f.loja }}</td><td>{{ f.sku or f.anuncio }}</td><td class="n">{{ f.valor|brl }}</td>
<td><select class="vn" data-origem="{{ f.origem }}" data-sug="{{ f.sugestao }}" style="max-width:280px"><option value="">— manter separado —</option>
{% for o in ex.opcoes %}<option value="{{ o.chave }}" {% if o.chave == f.sugestao %}selected{% endif %}>{{ o.nome }}{{ ' (SKU ' ~ o.chave ~ ')' if not o.chave.startswith('id:') else '' }}</option>{% endfor %}</select>
{% if f.sugestao %}<span class="nota">sugestão — confirme</span>{% endif %}</td></tr>{% endfor %}</table></div>{% endif %}
<div class="card"><h2>Produtos</h2><p class="nota">Variações e lojas do mesmo produto padrão aparecem somadas numa linha. O custo da linha do produto é o <b>padrão</b> (se o SKU já está em Produtos com custo, ele vem preenchido). Clique em ▸ para ver as variações e dar a cada uma o seu custo, quando o tamanho ou modelo muda o preço; a variação sem custo próprio usa o padrão. Variação terminada em vírgula e número (ex.: “Roxo Lavanda,5”) é pacote de quantidade: o custo informado é o de <b>1 peça</b> e o sistema multiplica pela quantidade. “Excluir” tira o produto desta lista e dos totais (dá para restaurar no fim da página).</p>
<div style="overflow:auto"><table><tr><th>Produto</th><th>Lojas</th><th class="n">Unid.</th><th class="n">Faturamento</th><th class="n">Preço médio</th><th class="n" title="Preço mínimo para dar a margem desejada, descontando custo, comissão e imposto">Preço mín. ({{ T.margem_alvo }}%)</th><th class="n">Custo unit. (R$)</th><th class="n">Custo</th><th class="n">Comissão</th><th class="n">Imposto</th><th class="n">Lucro</th><th class="n">Margem</th><th></th></tr>
{% for r in prods %}<tr class="pai" data-k="{{ r.chave }}"><td style="max-width:340px">{% if r.fontes|length > 1 %}<button class="btn sec sm tg" data-k="{{ r.chave }}" title="Ver variações">▸ {{ r.fontes|length }}</button> {% endif %}<b>{{ r.produto }}</b><br><span class="nota">{{ 'SKU ' ~ r.sku if r.sku else 'sem SKU' }}{{ '' if r.padrao else ' · fora do catálogo' }}{{ ' · custos diferentes nas variações' if r.varia else '' }}</span></td>
<td>{{ r.lojas|join(', ') }}</td><td class="n">{{ r.unidades }}</td><td class="n">{{ r.valor|brl }}</td>
<td class="n">{{ r.preco_atual|brl }}</td><td class="n" style="{{ 'color:#b91c1c;font-weight:600' if r.abaixo else '' }}">{{ (r.preco_min|brl ~ (' ⚠' if r.abaixo else '')) if r.preco_min else '—' }}</td>
<td class="n"><input class="cu" title="Custo padrão do produto: vale para as variações que não tiverem custo próprio" data-chave="{{ r.chave }}" value="{{ r.custo_fmt }}" placeholder="0,00" inputmode="decimal" style="width:80px;text-align:right{{ ';border-color:#dc2626' if r.falta_custo and not r.custo_unit else '' }}"></td>
<td class="n">{{ r.custo|brl }}</td><td class="n">{{ r.comissao|brl }}</td><td class="n">{{ r.imposto|brl }}</td><td class="n" style="color:{{ '#15803d' if r.lucro >= 0 else '#b91c1c' }}">{{ r.lucro|brl }}</td><td class="n">{{ '%.1f'|format(r.margem) }}%</td>
<td><button class="btn red sm bx" data-chave="{{ r.chave }}" data-nome="{{ r.produto[:50] }}">Excluir</button></td></tr>
{% if r.fontes|length > 1 %}{% for f in r.fontes %}<tr class="vr" data-p="{{ r.chave }}" style="display:none;background:rgba(127,127,127,.08)"><td style="padding-left:28px;max-width:340px">↳ {{ f.rotulo }}<br><span class="nota">{{ f.sub }}</span></td>
<td>{{ f.loja }}</td><td class="n">{{ f.unidades }}</td><td class="n">{{ f.valor|brl }}</td>
<td class="n">{{ f.preco_atual|brl }}</td><td class="n" style="{{ 'color:#b91c1c;font-weight:600' if f.abaixo else '' }}">{{ (f.preco_min|brl ~ (' ⚠' if f.abaixo else '')) if f.preco_min else '—' }}</td>
<td class="n"><input class="cu" title="Custo desta variação. Vazio = usa o custo padrão do produto" data-chave="{{ f.custo_chave }}" value="{{ f.custo_fmt }}" placeholder="{{ f.custo_herdado or '0,00' }}" inputmode="decimal" style="width:80px;text-align:right{{ ';border-color:#dc2626' if not f.custo_unit else '' }}">{% if f.qtd > 1 %}<br><span class="nota">× {{ f.qtd }} = {{ f.custo_unit|brl }} por venda</span>{% endif %}</td>
<td class="n">{{ f.custo|brl }}</td><td class="n">{{ f.comissao|brl }}</td><td class="n">{{ f.imposto|brl }}</td><td class="n" style="color:{{ '#15803d' if f.lucro >= 0 else '#b91c1c' }}">{{ f.lucro|brl }}</td><td class="n">{{ '%.1f'|format(f.margem) }}%</td><td></td></tr>{% endfor %}{% endif %}{% endfor %}
</table></div></div>
{% if ex.ignorados %}<div class="card"><h2>Excluídos ({{ ex.ignorados|length }})</h2><table>{% for x in ex.ignorados %}<tr><td>{{ x.nome }}</td><td class="n">{{ x.valor|brl }}</td><td><button class="btn sec sm br" data-chave="{{ x.chave }}">Restaurar</button></td></tr>{% endfor %}</table></div>{% endif %}
<script>
async function salvaMkt(url, corpo) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corpo)});
  const j = await r.json();
  if (!j.ok) { alert((j.erros || ['Erro']).join('\n')); return; }
  location.reload();
}
function abertos() { try { return JSON.parse(localStorage.getItem('mktAb') || '[]'); } catch (e) { return []; } }
function mostra(k, v) { document.querySelectorAll('.vr').forEach(r => { if (r.dataset.p === k) r.style.display = v ? '' : 'none'; }); }
document.querySelectorAll('.tg').forEach(b => {
  if (abertos().includes(b.dataset.k)) { mostra(b.dataset.k, true); b.textContent = b.textContent.replace('▸', '▾'); }
  b.addEventListener('click', () => {
    const k = b.dataset.k, ab = abertos(), on = !ab.includes(k);
    mostra(k, on); b.textContent = b.textContent.replace(on ? '▸' : '▾', on ? '▾' : '▸');
    try { localStorage.setItem('mktAb', JSON.stringify(on ? ab.concat(k) : ab.filter(x => x !== k))); } catch (e) {}
  });
});
document.querySelectorAll('.cu').forEach(e => e.addEventListener('change', () => salvaMkt('/api/marketplaces/custo', {chave: e.dataset.chave, custo: e.value})));
document.querySelectorAll('.vn').forEach(e => e.addEventListener('change', () => salvaMkt('/api/marketplaces/vinculo', {origem: e.dataset.origem, grupo: e.value})));
document.querySelectorAll('.bx').forEach(e => e.addEventListener('click', () => { if (confirm('Excluir "' + e.dataset.nome + '" desta lista e dos totais? Dá para restaurar depois.')) salvaMkt('/api/marketplaces/excluir', {chave: e.dataset.chave}); }));
document.querySelectorAll('.br').forEach(e => e.addEventListener('click', () => salvaMkt('/api/marketplaces/excluir', {chave: e.dataset.chave, restaurar: true})));
const bs = document.getElementById('bSug');
if (bs) bs.addEventListener('click', () => { const pares = [...document.querySelectorAll('.vn')].filter(e => e.dataset.sug).map(e => ({origem: e.dataset.origem, grupo: e.dataset.sug})); if (pares.length) salvaMkt('/api/marketplaces/vinculo', {pares}); else alert('Sem sugestões.'); });
const ma = document.getElementById('margemAlvo'); if (ma) ma.addEventListener('change', () => salvaMkt('/api/marketplaces/margem', {valor: ma.value}));
document.querySelectorAll('.tx').forEach(e => e.addEventListener('change', () => salvaMkt('/api/marketplaces/taxa', {loja: e.dataset.loja, campo: e.dataset.campo, valor: e.value})));
</script>
{% endif %}
"""

MKT_DADOS_HTML = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><div><h1>Importar relatórios e catálogo</h1><span class="mut">Relatórios do UpSeller para a página Marketplaces.</span></div><a class="btn sec" href="/marketplaces">← Voltar</a></div>
<div class="card"><h2>Importar relatório do UpSeller</h2>
<form method="post" action="/marketplaces/importar" enctype="multipart/form-data" class="bar" style="flex-wrap:wrap;align-items:end">
<div><label>Arquivo Excel (.xlsx)</label><input type="file" name="arquivo" accept=".xlsx" required></div>
<div><label>Início (se o nome do arquivo não trouxer)</label><input type="date" name="ini"></div>
<div><label>Fim</label><input type="date" name="fim"></div>
<button class="btn">Importar</button></form>
<p class="nota">O sistema reconhece sozinho se é o relatório por loja ou por produto. O período é lido do nome do arquivo (ex.: …20261001-20261025…). Importar de novo o mesmo período substitui os números, sem duplicar.</p></div>

<div class="card"><h2>Catálogo de referência</h2>
<p class="nota">Importe a planilha dos seus produtos (a exportação de produtos da Shopee/UpSeller com “ID do Produto” e “SKU de referência”). Cada SKU vira o <b>produto padrão</b>: variações e anúncios das outras lojas (pelo ID do anúncio ou pelo SKU) são somados numa venda única. A descrição não é usada.</p>
<div class="bar" style="margin:0;align-items:center"><span><b>{{ n_cat }}</b> anúncio(s) no catálogo</span>{% if n_cat %}<form method="post" action="/marketplaces/limpar-catalogo" style="margin:0" onsubmit="return confirm('Remover o catálogo e os vínculos manuais?')"><button class="btn red sm">Remover catálogo</button></form>{% endif %}</div></div>
<div class="card"><h2>Relatórios já importados</h2>
{% if not resumo %}<p class="mut">Nada importado ainda.</p>{% else %}<table><tr><th>Tipo</th><th>Período</th><th class="n">Linhas</th></tr>
{% for r in resumo %}<tr><td>{{ r.tipo }}</td><td>{{ r.ini|dia }} a {{ r.fim|dia }}</td><td class="n">{{ r.n }}</td></tr>{% endfor %}</table>{% endif %}</div>
{% endblock %}
"""

MKT_RANKING_HTML = r"""{% if not lp %}<div class="card"><p class="mut">Importe um relatório “Vendas por Produtos” ou “Vendas por variante” para ver o ranking.</p></div>{% else %}
<div class="card bar" style="margin:0 0 12px"><form method="get" class="bar" style="margin:0"><input type="hidden" name="aba" value="ranking"><label style="margin:0">Período</label><select name="p" onchange="this.form.submit()">
{% for p in lp %}<option value="{{ p.ini }}|{{ p.fim }}" {% if p.ini == ini and p.fim == fim %}selected{% endif %}>{{ p.ini|dia }} a {{ p.fim|dia }}</option>{% endfor %}</select></form></div>
{% if rk.sem_custo %}<div class="flash erro" style="margin-bottom:12px">{{ rk.sem_custo }} produto(s) sem custo cadastrado entram no ranking como se o custo fosse zero, então o lucro deles aparece maior do que é. Preencha os custos em <a href="/marketplaces?aba=lucro&p={{ ini }}|{{ fim }}">Produtos e lucro</a>.</div>{% endif %}
{% macro tabela(lista, vazio) %}{% if lista %}<div style="overflow:auto"><table><tr><th>Produto</th><th>Lojas</th><th class="n">Unid.</th><th class="n">Faturamento</th><th class="n">Lucro</th><th class="n">Margem</th></tr>
{% for r in lista %}<tr><td style="max-width:360px">{{ r.produto }}<br><span class="nota">{{ 'SKU ' ~ r.sku if r.sku else 'sem SKU' }}</span></td><td>{{ r.lojas|join(', ') }}</td><td class="n">{{ r.unidades }}</td><td class="n">{{ r.valor|brl }}</td>
<td class="n" style="color:{{ '#15803d' if r.lucro >= 0 else '#b91c1c' }}">{{ r.lucro|brl }}</td><td class="n">{{ '%.1f'|format(r.margem) }}%</td></tr>{% endfor %}</table></div>{% else %}<p class="mut">{{ vazio }}</p>{% endif %}{% endmacro %}
<div class="card"><h2>🏆 Mais lucrativos</h2>{{ tabela(rk.top, 'Sem dados.') }}</div>
<div class="card"><h2>⚠️ Dão prejuízo</h2>{{ tabela(rk.prejuizo, 'Nenhum produto com prejuízo neste período. 👍') }}</div>
<div class="card"><h2>🐌 Vendem muito, rendem pouco</h2><p class="nota">Entre os 15 que mais faturam, os que têm margem abaixo da média do período ({{ '%.1f'|format(rk.margem_media) }}%).</p>{{ tabela(rk.baixa, 'Nenhum dos mais vendidos está abaixo da média.') }}</div>
{% if rk.variacoes %}<div class="card"><h2>🎨 Variações mais vendidas</h2><table><tr><th>Variação</th><th>Produto</th><th>Loja</th><th class="n">Unid.</th><th class="n">Faturamento</th><th class="n">Margem</th></tr>
{% for f in rk.variacoes %}<tr><td>{{ f.rotulo }}</td><td style="max-width:260px">{{ f.pai }}</td><td>{{ f.loja }}</td><td class="n">{{ f.unidades }}</td><td class="n">{{ f.valor|brl }}</td><td class="n">{{ '%.1f'|format(f.margem) }}%</td></tr>{% endfor %}</table></div>{% endif %}
{% endif %}
"""

FOLLOWUP_HTML = r"""{% extends "base.html" %}{% block conteudo %}
<div class="cab"><div><h1>Orçamentos parados</h1><span class="mut">Orçamentos (ainda não aprovados) sem resposta. Envie um lembrete pelo WhatsApp: o sistema abre a conversa com a mensagem pronta e registra que você lembrou o cliente.</span></div></div>
<div class="bar" style="margin:0 0 12px">{% for k, rot in [('pendentes','Para lembrar'),('lembrados','Já lembrados'),('dispensados','Não lembrar'),('todos','Todos abertos')] %}<a class="btn {{ '' if aba == k else 'sec' }} sm" href="{{ url_for('followup', aba=k) }}">{{ rot }} ({{ cont[k] }})</a>{% endfor %}</div>
<div class="card">{% if rows %}<table><tr><th>#</th><th>Cliente</th><th>Peça</th><th class="n">Valor</th><th>Parado há</th><th>Último lembrete</th><th></th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('orcamento_ver', oid=r.id) }}">{{ r.id }}</a></td><td>{{ r.cliente }}<br><span class="nota">{{ r.telefone or 'sem telefone' }}</span></td><td>{{ r.titulo or '' }}</td><td class="n">{{ r.total|brl }}</td>
<td>{{ r.dias }} dia(s)</td><td>{% if r.followup_em %}{{ r.followup_em|dt }}{% else %}<span class="mut">—</span>{% endif %}</td>
<td class="n" style="white-space:nowrap">
<form class="inline" method="post" target="_blank" action="{{ url_for('followup_enviar', oid=r.id) }}"><button class="btn sm" style="background:#16a34a" {{ '' if numero_whatsapp(r.telefone) else 'disabled title="Cliente sem telefone válido"' }}>💬 Lembrar</button></form>
<form class="inline" method="post" action="{{ url_for('followup_estado', oid=r.id) }}"><input type="hidden" name="acao" value="lembrado"><button class="btn sec sm" title="Já falei com o cliente por outro meio">Já falei</button></form>
{% if r.followup_off %}<form class="inline" method="post" action="{{ url_for('followup_estado', oid=r.id) }}"><input type="hidden" name="acao" value="pendente"><button class="btn sec sm">Voltar</button></form>
{% else %}<form class="inline" method="post" action="{{ url_for('followup_estado', oid=r.id) }}"><input type="hidden" name="acao" value="dispensar"><button class="btn sec sm">Não lembrar</button></form>{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p class="mut">{{ 'Nenhum orçamento parado. 👍' if aba == 'pendentes' else 'Nada por aqui.' }}</p>{% endif %}</div>
<div class="card"><h2>Configuração</h2>
<form method="post" action="{{ url_for('followup_config') }}">
<label>Considerar parado depois de quantos dias</label><input name="dias" value="{{ dias }}" inputmode="numeric" style="width:90px">
<p class="nota">Conta a partir da data do orçamento e, depois de um lembrete, a partir do lembrete.</p>
<label>Mensagem</label><textarea name="msg" rows="5" style="width:100%">{{ modelo }}</textarea>
<p class="nota">Use {nome} (primeiro nome do cliente), {empresa}, {os} (número do orçamento) e {valor} (total).</p>
<button class="btn">Salvar</button></form></div>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "followup.html": FOLLOWUP_HTML,
    "mkt_ranking.html": MKT_RANKING_HTML,
    "mkt_dados.html": MKT_DADOS_HTML,
    "mkt_lucro.html": MKT_LUCRO_HTML,
    "marketplaces.html": MARKETPLACES_HTML,
    "seguranca.html": SEGURANCA,
    "login.html": LOGIN,
    "exportar.html": EXPORTAR,
    "compras.html": COMPRAS,
    "relatorios.html": RELATORIOS,
    "entregas.html": ENTREGAS,
    "contas.html": CONTAS,
    "backups.html": BACKUPS,
    "base.html": BASE, "dashboard.html": DASHBOARD, "financeiro.html": FINANCEIRO, "empresa.html": EMPRESA, "categorias.html": CATEGORIAS, "imprimir.html": IMPRIMIR, "avaliacoes.html": AVALIACOES, "bancos.html": BANCOS, "bancos_categorias.html": BANCOS_CAT, "extrato_envio.html": EXTRATO_ENVIO, "extrato_ver.html": EXTRATO_VER, "duplicidades.html": DUPLICIDADES, "clientes.html": CLIENTES,
    "cliente_ver.html": CLIENTE_VER, "cliente_form.html": CLIENTE_FORM,
    "produtos.html": PRODUTOS, "produto_form.html": PRODUTO_FORM, "produto_mov.html": PRODUTO_MOV,
    "orcamentos.html": ORCAMENTOS, "orcamento_form.html": ORCAMENTO_FORM,
    "orcamento_ver.html": ORCAMENTO_VER, "vendas.html": VENDAS,
    "venda_nova.html": VENDA_NOVA, "venda_ver.html": VENDA_VER,
    "agenda.html": AGENDA, "agenda_form.html": AGENDA_FORM,
})

iniciar_db()
carregar_segredo()


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
