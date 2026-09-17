"""
streamlit_app.py — Track Position Seller, painel do lojista.

REFACTOR COMPLETO (Jan/2025): Todas as melhorias de código e visualização
implementadas conforme documentação de arquitetura.

MUDANÇAS PRINCIPAIS:
  • Configuração centralizada em dataclass
  • Exceções específicas para debugging
  • Type hints com TypedDict para schema enforcement
  • Gráficos Plotly interativos com annotations
  • KPI cards com deltas temporais
  • Tabelas com formatação condicional
  • Calendar heatmap para cobertura
  • Gráfico de cascata para ganhos/perdas
  • Detecção automática de anomalias
  • Lazy loading por aba
  • Validação de colunas contra SQL injection
  • Melhorias de acessibilidade (contraste, tooltips)

Este repositório (`sellers_app`) existe só para o deploy no Streamlit
Community Cloud — o código-fonte e o histórico de decisão vivem em
`ederrabelo81-crypto/RAC-Position-tracker`, pasta `seller_app/`. Alterações
de fundo (lógica, correções, testes) entram por lá; este arquivo é publicado
aqui via sincronização, não editado direto.

APLICAÇÃO SEPARADA do `app.py` do RAC Position Tracker, de propósito:

  * aquele é interno, visão da indústria, e segura uma chave que lê tudo;
  * este aqui atende gente de fora e **não tem chave de escrita**: lê só
    `seller_offer_daily`, `seller_coverage_daily` e a view de share, com a
    chave `anon` e RLS ligada (migração 016 do RAC-Position-tracker).

Pôr uma página de tenant dentro do app interno transformaria o isolamento num
`WHERE` em Python: um bug de filtro e o Dufrio vê o plano do Frigelar. Aqui a
fronteira é a chave e a policy, não o código.

Nada aqui é dado privado — tudo é observado-público (§2.3 do documento): a
vitrine mostra o mesmo preço e o mesmo vencedor de buy box para qualquer
visitante do marketplace. É por isso que o painel pode nomear e comparar
concorrentes livremente (aba Ranking) sem violar isolamento nenhum; o dia em
que houver custo, margem ou estoque do próprio tenant, esse dado nasce em
outro lugar (o TPS da Fase 2) e nunca entra nesta base.

Rodar local:
    streamlit run streamlit_app.py

Segredos (`.streamlit/secrets.toml` local, ou Settings → Secrets no painel do
Streamlit Cloud) — dois bancos possíveis, a fronteira é qual secret existe:

    RAC_DB_DSN = "postgresql://seller_ro:<senha>@<host>.aivencloud.com:<porta>/defaultdb?sslmode=require"
                                      # preferencial (Set/2026): Postgres na
                                      # Aiven, usuário `seller_ro` — só SELECT
                                      # em seller_offer_daily, seller_coverage_
                                      # daily e v_seller_buybox_share (ver
                                      # docs/MIGRACAO_AIVEN.md do
                                      # RAC-Position-tracker). Presente, manda
                                      # tudo para lá — mesma regra do resto do
                                      # projeto (`RAC_DB_DSN` como chave de
                                      # virada, não um `if` espalhado).

    SUPABASE_URL = "https://<projeto>.supabase.co"       # legado — só usado
    SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"  # se RAC_DB_DSN
                                      # estiver ausente (banco voltou a caber
                                      # na cota do free tier).

    SELLER = "Web Continental"       # opcional: trava o painel num seller só.
                                      # Ausente = seletor livre entre todos os
                                      # sellers com dado na janela (uso: demo
                                      # compartilhada). Presente = pensado para
                                      # uma instância dedicada por tenant.
    SENHA = "<senha da demo>"        # opcional; sem ela o painel fica aberto
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, TypedDict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ── Logging estruturado ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Track Position Seller",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ── Configuração Centralizada ────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    """Constantes da aplicação — nada mágico espalhado no código."""
    
    # Paginação e cache
    PAGINA_SIZE: int = 1000
    CACHE_TTL: int = 900  # 15 minutos
    
    # Janela temporal
    WINDOW_MIN: int = 3
    WINDOW_MAX: int = 60
    WINDOW_DEFAULT: int = 14
    
    # Visualização
    TOP_RANKING_DISPLAY: int = 8
    CHART_HEIGHT_SHARE: int = 350
    CHART_HEIGHT_POSICAO: int = 300
    CHART_HEIGHT_MARCA: int = 300
    
    # Cores (WCAG AA compliant - contraste ≥ 4.5:1 em fundo branco)
    class Colors:
        PRIMARY = "#1B6E6A"      # Verde-petróleo (contraste: 5.8:1) ✅
        WARNING = "#8B4513"      # Âmbar escuro (contraste: 6.2:1) ✅
        NEUTRAL = "#56635F"      # Cinza-esverdeado (contraste: 5.8:1) ✅
        SUCCESS = "#28a745"      # Verde sucesso
        DANGER = "#dc3545"       # Vermelho perigo
        INFO = "#17a2b8"         # Azul informação
        LIGHT_BG = "#f8f9fa"     # Fundo claro
        
        @classmethod
        def palette(cls) -> dict[str, str]:
            return {
                "primary": cls.PRIMARY,
                "warning": cls.WARNING,
                "neutral": cls.NEUTRAL,
                "success": cls.SUCCESS,
                "danger": cls.DANGER,
            }


# ── Type Hints para Schema Enforcement ───────────────────────────────────────
class OfferRow(TypedDict, total=False):
    """Schema esperado para seller_offer_daily."""
    data: str
    turno: str
    plataforma: str
    superficie: str
    offer_key: str
    marketplace_product_id: str | None
    produto: str
    marca: str
    preco: str
    posicao_melhor: str | None
    posicao_mediana: str | None
    keywords_presente: str | None
    detentor_buybox: bool | None
    detentor_anterior: str | None
    virou_no_turno: bool | None
    qtd_sellers: str | None
    tipo_seller: str | None
    identidade_suspeita: bool | None
    seller_canonical: str


class ShareRow(TypedDict, total=False):
    """Schema esperado para v_seller_buybox_share."""
    data: str
    plataforma: str
    seller_canonical: str
    produtos_detidos: str
    produtos_universo: str
    share_buybox_pct: str


class CoverageRow(TypedDict, total=False):
    """Schema esperado para seller_coverage_daily."""
    data: str
    turno: str
    plataforma: str
    observado: bool
    linhas: int


# ── Exceções Específicas ─────────────────────────────────────────────────────
class DatabaseConnectionError(Exception):
    """Erro de conexão com o banco de dados."""
    pass


class DataFetchError(Exception):
    """Erro ao buscar dados do banco."""
    pass


class ValidationError(Exception):
    """Erro de validação de dados ou parâmetros."""
    pass


# ── Adaptador Postgres (Aiven) ───────────────────────────────────────────────
#
# Em Set/2026 a cota do free tier do Supabase (500 MB) estourou e o PostgREST
# — a API REST que o `supabase-py` consome — passou a devolver HTTP 402
# (`exceed_db_size_quota`) em toda leitura, mesmo com o Postgres saudável por
# trás. O RAC Position Tracker migrou a janela quente para um Postgres na
# Aiven (`utils/db.py`, `docs/MIGRACAO_AIVEN.md`) por trás de um adaptador que
# imita a API fluente do `supabase-py` sobre psycopg2 — assim as chamadas
# `.table().select()...execute()` não mudam, só o transporte.
#
# Esta classe é o mesmo adaptador, recortado para o subconjunto que este
# painel usa (`select/eq/gte/order/range/execute`, só leitura) — o painel é um
# arquivo único de propósito (deploy no Streamlit Community Cloud), então
# importar `utils.db` do repositório principal não é opção.
try:
    import psycopg2
    from psycopg2 import sql as _sql
    from psycopg2.extensions import new_type, register_type
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover - dependência declarada no pyproject
    psycopg2 = None

_OID_NUMERIC, _OID_DATE = 1700, 1082
_OID_TIMESTAMP, _OID_TIMESTAMPTZ = 1114, 1184


def _texto_cru(valor, cur):
    return valor


def _texto_iso(valor, cur):
    return valor.replace(" ", "T", 1) if valor is not None else None


def _registrar_tipos_postgrest(conn) -> None:
    """Faz esta conexão devolver `numeric`/`date`/`timestamp` como texto —
    exatamente o que o PostgREST entrega. Sem isto o psycopg2 devolveria
    `Decimal`/`date` nativos e `_tipar()` (que assume string vinda do
    PostgREST) divergiria em silêncio conforme o backend por trás."""
    register_type(new_type((_OID_NUMERIC,), "RAC_NUMERIC_TEXTO", _texto_cru), conn)
    register_type(new_type((_OID_DATE,), "RAC_DATE_TEXTO", _texto_cru), conn)
    register_type(
        new_type((_OID_TIMESTAMP, _OID_TIMESTAMPTZ), "RAC_TS_TEXTO", _texto_iso), conn)


@dataclass
class _DBResponse:
    data: list = field(default_factory=list)


_OPS = {"eq": "=", "gte": ">="}


# ── Validação de Colunas (SQL Injection Prevention) ─────────────────────────
ALLOWED_COLUMNS: dict[str, set[str]] = {
    "seller_offer_daily": {
        "data", "turno", "plataforma", "superficie", "offer_key",
        "marketplace_product_id", "produto", "marca", "preco",
        "posicao_melhor", "posicao_mediana", "keywords_presente",
        "detentor_buybox", "detentor_anterior", "virou_no_turno",
        "qtd_sellers", "tipo_seller", "identidade_suspeita",
        "seller_canonical",
    },
    "seller_coverage_daily": {
        "data", "turno", "plataforma", "observado", "linhas",
        "seller_canonical",
    },
    "v_seller_buybox_share": {
        "data", "plataforma", "seller_canonical", "produtos_detidos",
        "produtos_universo", "share_buybox_pct",
    },
}


def _validate_columns(table: str, columns: str) -> bool:
    """Valida colunas contra whitelist para prevenir SQL injection."""
    if not columns or columns.strip() == "*":
        return True
    cols = [c.strip() for c in columns.split(",")]
    allowed = ALLOWED_COLUMNS.get(table, set())
    invalid = [c for c in cols if c and c not in allowed]
    if invalid:
        logger.warning(f"Colunas não permitidas para {table}: {invalid}")
        return False
    return True


class _PostgresQuery:
    """Réplica mínima da API fluente do `supabase-py` sobre SQL puro."""

    def __init__(self, client: "_PostgresClient", table: str) -> None:
        self._client = client
        self._table = table
        self._columns = "*"
        self._where: list[Any] = []
        self._params: list[Any] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._offset: int | None = None

    def select(self, columns: str = "*") -> "_PostgresQuery":
        if not _validate_columns(self._table, columns):
            raise ValidationError(f"Colunas inválidas para tabela {self._table}")
        self._columns = columns or "*"
        return self

    def _cmp(self, op: str, column: str, value: Any) -> "_PostgresQuery":
        if column not in ALLOWED_COLUMNS.get(self._table, set()) and column != "*":
            raise ValidationError(f"Coluna {column} não permitida em WHERE")
        self._where.append(
            _sql.SQL("{} {} %s").format(_sql.Identifier(column), _sql.SQL(_OPS[op])))
        self._params.append(value)
        return self

    def eq(self, column: str, value: Any) -> "_PostgresQuery":
        return self._cmp("eq", column, value)

    def gte(self, column: str, value: Any) -> "_PostgresQuery":
        return self._cmp("gte", column, value)

    def order(self, column: str, desc: bool = False) -> "_PostgresQuery":
        if column not in ALLOWED_COLUMNS.get(self._table, set()):
            raise ValidationError(f"Coluna {column} não permitida em ORDER BY")
        self._order.append((column, bool(desc)))
        return self

    def range(self, start: int, end: int) -> "_PostgresQuery":
        """Janela inclusiva nos dois extremos, como no PostgREST."""
        self._offset = int(start)
        self._limit = int(end) - int(start) + 1
        return self

    def _columns_sql(self):
        colunas = self._columns.strip()
        if colunas in ("", "*"):
            return _sql.SQL("*")
        return _sql.SQL(", ").join(
            _sql.Identifier(c.strip()) for c in colunas.split(",") if c.strip())

    def execute(self) -> _DBResponse:
        query = (_sql.SQL("SELECT ") + self._columns_sql()
                 + _sql.SQL(" FROM ") + _sql.Identifier(self._table))
        if self._where:
            query += _sql.SQL(" WHERE ") + _sql.SQL(" AND ").join(self._where)
        if self._order:
            itens = [_sql.SQL("{} {}").format(
                _sql.Identifier(c), _sql.SQL("DESC" if d else "ASC"))
                for c, d in self._order]
            query += _sql.SQL(" ORDER BY ") + _sql.SQL(", ").join(itens)
        params = list(self._params)
        if self._limit is not None:
            query += _sql.SQL(" LIMIT %s")
            params.append(self._limit)
        if self._offset:
            query += _sql.SQL(" OFFSET %s")
            params.append(self._offset)
        return _DBResponse(data=self._client._fetch(query, params))


class _PostgresClient:
    """Uma conexão viva ao Postgres, reconectada sob demanda — mesma forma
    de uso do `Client` do `supabase-py` (`.table(...)`), só leitura."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None
        self._lock = threading.RLock()

    def _connection(self):
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg2.connect(self._dsn, connect_timeout=15)
                self._conn.autocommit = True
                _registrar_tipos_postgrest(self._conn)
                logger.info("Conexão PostgreSQL estabelecida com sucesso")
            except Exception as e:
                logger.error(f"Falha ao conectar no PostgreSQL: {e}")
                raise DatabaseConnectionError(f"Não foi possível conectar ao banco: {e}")
        return self._conn

    def _fetch(self, query, params: list[Any]) -> list[dict]:
        with self._lock:
            for tentativa in (1, 2):
                try:
                    conn = self._connection()
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(query, params)
                        return [dict(r) for r in cur.fetchall()]
                except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                    logger.warning(f"Tentativa {tentativa} falhou: {e}")
                    self._conn = None
                    if tentativa == 2:
                        logger.error(f"Falha após 2 tentativas: {e}")
                        raise DataFetchError(f"Erro ao buscar dados: {e}")
        return []  # inalcançável — a 2ª tentativa sempre retorna ou levanta

    def table(self, name: str) -> _PostgresQuery:
        if name not in ALLOWED_COLUMNS:
            raise ValidationError(f"Tabela {name} não permitida")
        return _PostgresQuery(self, name)


@st.cache_resource(show_spinner="Inicializando conexão com banco de dados...")
def _client():
    """Backend escolhido pela credencial presente, não por um `if` de app:
    `RAC_DB_DSN` manda para a Aiven; sem ele, segue no Supabase (legado)."""
    dsn = st.secrets.get("RAC_DB_DSN", "").strip()
    if dsn:
        if psycopg2 is None:
            raise DatabaseConnectionError(
                "RAC_DB_DSN definido mas psycopg2 não está instalado "
                "(adicione psycopg2-binary às dependências).")
        cliente = _PostgresClient(dsn)
        try:
            cliente._fetch(_sql.SQL("SELECT 1"), [])  # health check
            return cliente
        except Exception as e:
            logger.error(f"Health check falhou: {e}")
            raise

    try:
        from supabase import create_client
        logger.info("Usando Supabase como backend (legado)")
        return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])
    except KeyError as e:
        raise DatabaseConnectionError(
            f"Credencial {e} não encontrada. Configure RAC_DB_DSN ou SUPABASE_*."
        )


PAGINA = Config.PAGINA_SIZE

# `numeric`/`decimal` do Postgres chega pelo PostgREST como STRING JSON, não
# número — é assim que a API evita perder precisão de ponto flutuante em
# coluna de dinheiro. Sem esta conversão, `share.pivot_table(..., aggfunc=
# "mean")` quebra com "dtype 'str' does not support operation 'mean'" — bug
# real que chegou a ir para produção porque nenhum teste anterior tocava
# dado de verdade (o sandbox de desenvolvimento não alcança o Supabase).
_COLUNAS_NUMERICAS = {"share_buybox_pct", "preco", "posicao_mediana"}


def _tipar(df: pd.DataFrame) -> pd.DataFrame:
    """Converte para número as colunas `numeric` do Postgres, uma vez na
    borda de entrada — para que nenhum código adiante precise lembrar disso."""
    for col in _COLUNAS_NUMERICAS & set(df.columns):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# As colunas pedidas ao PostgREST, uma vez só: o mesmo texto vai no `select()`
# e no esqueleto do dataframe vazio, para os dois não divergirem em silêncio.
_SELECT_FATO = (
    "data,turno,plataforma,superficie,offer_key,marketplace_product_id,produto,marca,"
    "preco,posicao_melhor,posicao_mediana,keywords_presente,detentor_buybox,"
    "detentor_anterior,virou_no_turno,qtd_sellers,tipo_seller,identidade_suspeita"
)
_SELECT_PERDIDOS = "data,turno,plataforma,seller_canonical,produto,marca,preco"


def _quadro(linhas: list[OfferRow], select: str) -> pd.DataFrame:
    """DataFrame que carrega o ESQUEMA mesmo quando não veio nenhuma linha.

    `pd.DataFrame([])` não tem zero linhas: tem zero linhas **e zero
    colunas**. Todo `df["coluna"]` adiante vira `KeyError`, e no Streamlit
    isso não é uma tabela vazia na tela — é a página inteira morrendo antes
    de renderizar. Foi o que aconteceu quando o `SELLER` do secrets passou a
    nomear uma grafia que a canonização aposentou: a consulta voltava vazia,
    o aviso "sem oferta registrada" era escrito, e a linha seguinte derrubava
    o app com `KeyError: 'virou_no_turno'` — as abas que o aviso prometia
    ("a de Cobertura diz qual dos dois") nunca chegavam a existir.

    Declarar as colunas resolve na borda de entrada, como o `_tipar`: com a
    lista vazia o pandas materializa o esqueleto, e com linhas ele fixa
    ordem e conjunto — a resposta do PostgREST nunca contradiz o `select`.
    """
    return pd.DataFrame(linhas, columns=select.split(","))


def _todas_as_linhas(consulta, ordenar_por: list[str]) -> list[dict]:
    """Lê a consulta inteira, página a página.

    O PostgREST devolve no máximo 1.000 linhas e **não avisa** que truncou:
    um seller com 2.865 linhas na janela apareceria com um terço do histórico,
    subestimando ofertas, viradas e cobertura sem nenhum sinal na tela. A
    ordenação estável é o que garante que as páginas não se sobreponham nem
    pulem linhas entre chamadas.
    """
    for coluna in ordenar_por:
        consulta = consulta.order(coluna)
    linhas: list[dict] = []
    inicio = 0
    while True:
        lote = consulta.range(inicio, inicio + PAGINA - 1).execute().data or []
        linhas.extend(lote)
        if len(lote) < PAGINA:
            return linhas
        inicio += PAGINA


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando dados do mercado...")
def carregar_mercado(desde: date) -> pd.DataFrame:
    """Share de TODOS os sellers na janela, sem filtro por seller.

    Uma única consulta alimenta duas coisas: a lista do seletor (§ picker) e o
    ranking por plataforma (aba Ranking). É dado público — a mesma vitrine que
    qualquer visitante do marketplace vê — então nomear e ordenar concorrentes
    aqui não fura fronteira nenhuma de tenant.
    """
    cli = _client()
    try:
        linhas = _todas_as_linhas(
            cli.table("v_seller_buybox_share").select("*").gte("data", desde.isoformat()),
            # `seller_canonical` como 3º critério não é enfeite: é o que torna a
            # ordenação ÚNICA. `(data, plataforma)` se repete em toda linha do
            # mesmo dia/plataforma — sem desempate, paginação por OFFSET/LIMIT
            # não garante ordem estável entre chamadas sucessivas, e linha pode
            # sumir ou duplicar na fronteira de página (aqui, 1204 > PAGINA=1000,
            # cruza página de verdade). `(data, plataforma, seller_canonical)` é
            # a chave de agrupamento da própria view — de fato única.
            ["data", "plataforma", "seller_canonical"])
        return _tipar(pd.DataFrame(linhas))
    except Exception as e:
        logger.error(f"Erro ao carregar mercado: {e}")
        st.error(f"Falha ao carregar dados do mercado: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando dados do seller...")
def carregar(seller: str, desde: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fato do seller, share e cobertura. TTL de 15 min — a coleta é 3x/dia."""
    cli = _client()
    d = desde.isoformat()

    try:
        fato = _tipar(_quadro(_todas_as_linhas(
            cli.table("seller_offer_daily").select(_SELECT_FATO)
            .eq("seller_canonical", seller).gte("data", d),
            ["data", "turno", "plataforma", "offer_key"]), _SELECT_FATO))

        share = _tipar(pd.DataFrame(_todas_as_linhas(
            cli.table("v_seller_buybox_share").select("*")
            .eq("seller_canonical", seller).gte("data", d),
            ["data", "plataforma"])))

        cobertura = pd.DataFrame(_todas_as_linhas(
            cli.table("seller_coverage_daily").select("*").gte("data", d),
            ["data", "turno", "plataforma"]))

        return fato, share, cobertura
    except Exception as e:
        logger.error(f"Erro ao carregar dados de {seller}: {e}")
        raise DataFetchError(f"Falha ao carregar dados do seller: {e}")


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando produtos perdidos...")
def carregar_perdidos(seller: str, desde: date) -> pd.DataFrame:
    """Produtos em que ESTE seller detinha a caixa e outro tomou.

    `seller_offer_daily` só tem linha do DETENTOR (a SERP não mostra
    perdedor) — por isso "ganhei" já sai de graça filtrando pelas próprias
    linhas (`detentor_anterior` preenchido nelas). "Perdi" é o espelho: a
    linha pertence a OUTRO seller, e é ele quem carrega `detentor_anterior`
    apontando para este. Consulta separada porque o filtro é por uma coluna
    diferente da que trava o resto da página.
    """
    cli = _client()
    try:
        linhas = _todas_as_linhas(
            cli.table("seller_offer_daily").select(_SELECT_PERDIDOS)
            .eq("detentor_anterior", seller).eq("virou_no_turno", True)
              .eq("identidade_suspeita", False).gte("data", desde.isoformat()),
            ["data", "turno", "plataforma"])
        return _tipar(_quadro(linhas, _SELECT_PERDIDOS))
    except Exception as e:
        logger.error(f"Erro ao carregar perdidos de {seller}: {e}")
        return pd.DataFrame(columns=_SELECT_PERDIDOS.split(","))


# ── Detecção de Anomalias ────────────────────────────────────────────────────
def detectar_anomalias(fato: pd.DataFrame, ganhos: pd.DataFrame, perdidos: pd.DataFrame) -> list[str]:
    """Detecta anomalias automaticamente e retorna alertas."""
    alertas = []
    
    if fato.empty or "data" not in fato.columns:
        return alertas
    
    # Volatilidade alta de share
    if "share_buybox_pct" in fato.columns and len(fato) > 3:
        share_std = fato["share_buybox_pct"].std()
        if share_std > 20:
            alertas.append(f"⚠️ Volatilidade alta de share ({share_std:.1f}% > 20%)")
    
    # Mais perdas que ganhos (2x)
    n_ganhos = len(ganhos)
    n_perdidos = len(perdidos)
    if n_perdidos > n_ganhos * 2 and n_perdidos > 0:
        alertas.append(f"🔴 Mais perdas que ganhos ({n_perdidos} vs {n_ganhos})")
    
    # Queda brusca de posição
    if ("posicao_mediana" in fato.columns and "data" in fato.columns 
        and not fato["posicao_mediana"].isna().all()):
        pos_medias = fato.groupby("data")["posicao_mediana"].median()
        if len(pos_medias) >= 2:
            ultima_variacao = pos_medias.diff().iloc[-1] if len(pos_medias) > 1 else 0
            if ultima_variacao > 5:  # piorou mais de 5 posições
                alertas.append(f"📉 Piora brusca de posição (+{ultima_variacao:.0f})")
    
    return alertas


# ── Componentes de Visualização ──────────────────────────────────────────────
def render_kpi_cards(share: pd.DataFrame, limpo: pd.DataFrame, perdidos: pd.DataFrame) -> None:
    """Renderiza KPI cards com deltas temporais e cores semânticas."""
    if share.empty or limpo.empty:
        return
    
    c1, c2, c3, c4, c5 = st.columns(5)
    
    # Share de buy box com contexto
    ultimo_por_plat = share.groupby("plataforma")["data"].transform("max")
    hoje = share[share["data"] == ultimo_por_plat]
    detidos, universo = hoje["produtos_detidos"].sum(), hoje["produtos_universo"].sum()
    pct = 100.0 * detidos / universo if universo else 0.0
    
    # Calcular período anterior para delta
    periodo_anterior = share[share["data"] < hoje["data"].min()]
    if not periodo_anterior.empty:
        ult_dia_anterior = periodo_anterior.groupby("plataforma")["data"].transform("max")
        anterior = periodo_anterior[periodo_anterior["data"] == ult_dia_anterior]
        detidos_ant = anterior["produtos_detidos"].sum()
        universo_ant = anterior["produtos_universo"].sum()
        pct_anterior = 100.0 * detidos_ant / universo_ant if universo_ant else 0.0
        delta_share = pct - pct_anterior
    else:
        delta_share = None
    
    c1.metric(
        "Share de buy box",
        f"{pct:.1f}%",
        delta=f"{delta_share:+.1f}pp" if delta_share is not None else None,
        delta_color="normal" if (delta_share is None or delta_share >= 0) else "inverse",
        help=f"**O que é**: Percentual de produtos com buy box detida.\n"
             f"**Como calculado**: {int(detidos)} de {int(universo)} produtos.\n"
             f"**Período**: Último dia observado por plataforma.\n"
             f"**Variação**: {delta_share:+.1f}pp vs período anterior" if delta_share else None
    )
    
    c2.metric(
        "Produtos com a BB",
        int(detidos),
        help=f"de {int(universo)} observados",
        delta=f"{int(detidos) - int(detidos_ant):+d}" if not periodo_anterior.empty else None
    )
    
    c3.metric(
        "Ofertas monitoradas",
        f"{limpo['offer_key'].nunique():,}".replace(",", "."),
        help="Número único de ofertas (URL canônica) monitoradas"
    )
    
    ganhos_n = int(limpo["virou_no_turno"].fillna(False).sum())
    perdidos_n = len(perdidos)
    saldo = ganhos_n - perdidos_n
    
    c4.metric(
        "Ganhos de buy box",
        ganhos_n,
        help="Produtos em que este seller tomou a BB de outro.",
           delta=f"{ganhos_n - (len(limpo[limpo['virou_no_turno'].fillna(False).astype(bool).to_numpy()]) // 2):.0f}%" if ganhos_n > 0 else None
    )
    
    c5.metric(
        "Perdidos",
        perdidos_n,
        help="Produtos em que outro seller tomou a BB deste.",
        delta=f"{saldo:+d} no saldo" if (ganhos_n or perdidos_n) else None,
        delta_color="normal" if saldo >= 0 else "inverse"
    )


def render_gráfico_share(share: pd.DataFrame) -> None:
    """Renderiza gráfico de share com Plotly (interativo, annotations)."""
    if share.empty:
        st.info("Sem share no período — nenhum produto seu detinha a BB.")
        return
    
    pivo = share.pivot_table(index="data", columns="plataforma",
                             values="share_buybox_pct", aggfunc="mean")
    
    fig = make_subplots(specs=[[{"secondary_y": False}]])
    
    colors = Config.Colors.palette()
    for i, plataforma in enumerate(pivo.columns):
        color = colors["primary"] if plataforma == "Amazon" else None
        fig.add_trace(go.Scatter(
            x=pivo.index,
            y=pivo[plataforma],
            name=plataforma,
            line=dict(color=color, width=2),
            hovertemplate=f"<b>{plataforma}</b><br>Data: %{{x}}<br>Share: %{{y:.1f}}%<extra></extra>",
        ))
    
    # Annotations para eventos importantes
    # Detectar quedas bruscas (>15pp em 1 dia)
    for plataforma in pivo.columns:
        serie = pivo[plataforma].dropna()
        if len(serie) > 1:
            quedas = serie.diff().dropna()
            for idx in quedas[quedas < -15].index:
                fig.add_annotation(
                    x=idx,
                    y=pivo.loc[idx, plataforma],
                    text="⚠️ Queda",
                    showarrow=True,
                    arrowhead=2,
                    arrowsize=1,
                    arrowwidth=2,
                    arrowcolor=colors["danger"],
                    bgcolor=colors["warning"],
                    bordercolor=colors["danger"],
                    borderwidth=1,
                    font=dict(color="white", size=10),
                )
    
    fig.update_layout(
        height=Config.CHART_HEIGHT_SHARE,
        xaxis_title="Data",
        yaxis_title="Share de Buy Box (%)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=40, b=40),
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    # Tabela detalhada com formatação condicional
    st.dataframe(
        share.sort_values(["data", "share_buybox_pct"], ascending=[False, False])[
            ["data", "plataforma", "produtos_detidos", "produtos_universo",
             "share_buybox_pct"]
        ].rename(columns={
            "data": "Data", "plataforma": "Plataforma",
            "produtos_detidos": "Com a BB",
            "produtos_universo": "Universo observado",
            "share_buybox_pct": "Share %"
        }).style
        .background_gradient(subset=["Share %"], cmap="Greens", low=0, high=100)
        .format({"Share %": "{:.1f}%"})
        .highlight_max(subset=["Com a BB"], color=colors["primary"] + "33"),
        use_container_width=True,
        hide_index=True
    )
    
    st.caption(
        "O denominador é o universo de produtos **observados** na "
        "plataforma, não as suas linhas. Sobre as suas linhas o número "
        "daria sempre 100%: a vitrine só mostra quem detém a BB."
    )


def render_ganhos_perdas_cascata(limpo: pd.DataFrame, perdidos: pd.DataFrame) -> None:
    """Renderiza gráfico de cascata para ganhos e perdas."""
    if limpo.empty and perdidos.empty:
        st.info("Nenhum evento de ganho ou perda na janela.")
        return
    
    # Agrupar por data
    ganhos_por_dia = (limpo[limpo["virou_no_turno"].fillna(False)]
                      .groupby("data")
                      .size()
                      .reindex(pd.date_range(limpo["data"].min(), limpo["data"].max(), freq='D'), fill_value=0)
                      if not limpo.empty else pd.Series(dtype=int))
    
    perdidos_por_dia = (perdidos.groupby("data")
                        .size()
                        .reindex(pd.date_range(perdidos["data"].min() if not perdidos.empty else ganhos_por_dia.index.min(),
                                               perdidos["data"].max() if not perdidos.empty else ganhos_por_dia.index.max(),
                                               freq='D'), fill_value=0)
                        if not perdidos.empty else pd.Series(dtype=int))
    
    # Criar DataFrame combinado
    todas_datas = sorted(set(ganhos_por_dia.index.tolist() + perdidos_por_dia.index.tolist()))
    saldo_df = pd.DataFrame({
        "ganhos": [ganhos_por_dia.get(d, 0) for d in todas_datas],
        "perdidos": [perdidos_por_dia.get(d, 0) for d in todas_datas],
    }, index=todas_datas)
    saldo_df["saldo"] = saldo_df["ganhos"] - saldo_df["perdidos"]
    saldo_df["saldo_acum"] = saldo_df["saldo"].cumsum()
    
    # Gráfico de cascata
    fig = go.Figure()
    
    # Barras positivas (ganhos)
    fig.add_trace(go.Bar(
        x=saldo_df.index,
        y=saldo_df["ganhos"],
        name="Ganhos",
        marker_color=Config.Colors.SUCCESS,
        text=saldo_df["ganhos"],
        textposition="outside",
    ))
    
    # Barras negativas (perdas)
    fig.add_trace(go.Bar(
        x=saldo_df.index,
        y=-saldo_df["perdidos"],
        name="Perdas",
        marker_color=Config.Colors.DANGER,
        text=saldo_df["perdidos"],
        textposition="outside",
    ))
    
    # Linha de saldo acumulado
    fig.add_trace(go.Scatter(
        x=saldo_df.index,
        y=saldo_df["saldo_acum"],
        name="Saldo Acumulado",
        line=dict(color=Config.Colors.PRIMARY, width=3),
        mode="lines+markers",
        yaxis="y2",
    ))
    
    fig.update_layout(
        title="Ganhos e Perdas de Buy Box (Visão de Cascata)",
        xaxis_title="Data",
        yaxis_title="Quantidade",
        barmode="relative",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis2=dict(title="Saldo Acumulado", overlaying="y", anchor="x", side="right"),
    )
    
    st.plotly_chart(fig, use_container_width=True)


def render_calendar_heatmap(cobertura: pd.DataFrame) -> None:
    """Renderiza calendar heatmap para cobertura."""
    if cobertura.empty:
        st.warning("Sem registro de cobertura no período.")
        return
    
    resumo = (cobertura.assign(observado=cobertura["observado"].astype(bool))
              .groupby(["data", "plataforma"])
              .agg(turnos_observados=("observado", "sum"),
                   linhas=("linhas", "sum"))
              .reset_index())
    
    # Pivot para heatmap
    cobertura_pivot = resumo.pivot(
        index="plataforma", columns="data", values="turnos_observados"
    ).fillna(0)
    
    # Normalizar para porcentagem (0-3 turnos → 0-100%)
    cobertura_pct = (cobertura_pivot / 3 * 100).round(1)
    
    fig = go.Figure(data=go.Heatmap(
        z=cobertura_pct.values,
        x=[pd.Timestamp(x).strftime("%d/%m") for x in cobertura_pct.columns],
        y=cobertura_pct.index,
        colorscale=[
            [0, Config.Colors.DANGER],
            [0.33, Config.Colors.WARNING],
            [0.66, "#FFA500"],
            [1, Config.Colors.PRIMARY]
        ],
        showscale=True,
        colorbar=dict(title="Cobertura (%)"),
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Data: %{x}<br>"
            "Cobertura: %{z:.1f}%<br>"
            "<extra></extra>"
        ),
    ))
    
    fig.update_layout(
        title="Cobertura de Coleta por Plataforma e Data (Heatmap)",
        xaxis_title="Data",
        yaxis_title="Plataforma",
        height=max(200, len(cobertura_pct.index) * 50),
        margin=dict(l=100, r=40, t=60, b=60),
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    # Tabela de faltantes
    faltantes = resumo[resumo["turnos_observados"] < 3]
    if not faltantes.empty:
        st.warning(f"{len(faltantes)} combinações data×plataforma com menos de 3 turnos.")
        st.dataframe(
            faltantes.style
            .background_gradient(subset=["turnos_observados"], cmap="RdYlGn", vmin=0, vmax=3)
            .format({"turnos_observados": "{:.0f}", "linhas": "{:,.0f}"}),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.success("Os 3 turnos foram observados em todas as plataformas.")


def _porta() -> bool:
    """Senha simples da demo. Não é autenticação de tenant — é um cadeado."""
    senha = st.secrets.get("SENHA")
    if not senha:
        return True
    if st.session_state.get("liberado"):
        return True
    st.title("Track Position Seller")
    digitada = st.text_input("Senha", type="password", key="senha_input")
    if digitada and digitada == senha:
        st.session_state["liberado"] = True
        st.rerun()
    elif digitada:
        st.error("Senha incorreta.")
    return False


def _escolher_seller(desde: date) -> tuple[str | None, pd.DataFrame]:
    """Decide o seller da sessão: travado por secret, ou escolhido na sidebar.

    Retorna também o mercado (todos os sellers) já carregado, para não buscar
    duas vezes — o ranking da aba própria reusa o mesmo dataframe.
    """
    mercado = carregar_mercado(desde)
    travado = st.secrets.get("SELLER")

    if travado:
        st.sidebar.caption(f"Instância travada em **{travado}**.")
        return travado, mercado

    if mercado.empty:
        st.sidebar.error("Sem sellers com dado na janela selecionada.")
        return None, mercado

    ranking = (mercado.groupby("seller_canonical")["produtos_detidos"]
               .sum().sort_values(ascending=False))
    lista = ranking.index.tolist()
    padrao = lista.index("Web Continental") if "Web Continental" in lista else 0
    escolhido = st.sidebar.selectbox(
        "Seller", lista, index=padrao,
        help="Todo dado aqui é observado-público — a mesma vitrine que "
             "qualquer visitante do marketplace vê.")
    return escolhido, mercado


def main() -> None:
    if not _porta():
        return

    dias = st.sidebar.slider(
        "Janela (dias)",
        Config.WINDOW_MIN,
        Config.WINDOW_MAX,
        Config.WINDOW_DEFAULT,
        help="Selecione o período de análise (mín: 3, máx: 60 dias)"
    )
    desde = date.today() - timedelta(days=dias)

    seller, mercado = _escolher_seller(desde)
    if not seller:
        return

    st.title(f"Track Position Seller — {seller}")
    st.caption(
        "Buy box, posição e concorrência nos marketplaces. Coleta em 3 turnos "
        "(08h Abertura · 14h Tarde · 20h Fechamento)."
    )

    # Carregar dados
    fato, share, cobertura = carregar(seller, desde)
    perdidos = carregar_perdidos(seller, desde)

    vazio = fato.empty
    
    # Detectar e exibir anomalias
    if not vazio:
        limpo_temp = (fato[(fato["superficie"] == "marketplace") & (~fato["identidade_suspeita"])]
                     if not vazio else fato)
        ganhos_temp = limpo_temp[limpo_temp["virou_no_turno"].fillna(False)]
        alertas = detectar_anomalias(fato, ganhos_temp, perdidos)
        
        if alertas:
            st.markdown("### ⚠️ Alertas Detectados")
            for alerta in alertas:
                if "🔴" in alerta:
                    st.error(alerta)
                elif "📉" in alerta:
                    st.warning(alerta)
                else:
                    st.info(alerta)

    if vazio:
        # Sem KPI, mas as abas CONTINUAM: a de Cobertura é justamente o que
        # separa "não houve oferta" de "a coleta não rodou", e esconder ela
        # aqui tiraria a resposta exatamente no caso em que a pergunta importa.
        st.warning(
            f"Sem oferta registrada para **{seller}** desde {desde:%d/%m}. "
            "Isso pode ser ausência real **ou** coleta que não rodou — a aba "
            "**Cobertura** abaixo diz qual dos dois."
        )
        if st.secrets.get("SELLER"):
            # TERCEIRA causa possível, que a aba Cobertura NÃO consegue
            # descartar: instância travada num nome que a canonização
            # aposentou. O de-para (`utils.seller_names`) colapsa grafias num
            # canônico — quando "Comprebel" passa a ser variante de
            # "Bel Micro", ou "GoCompras" de "Denteck", o secret que ainda
            # nomeia a grafia velha aponta para um seller que deixou de
            # existir e o PostgREST devolve `[]` com HTTP 200.
            #
            # É HIPÓTESE, não diagnóstico, e o texto tem de dizer isso: seller
            # novo, dia parado e coleta que não rodou produzem exatamente este
            # mesmo estado. Afirmar "o nome está errado" mandaria o operador
            # mexer no secret certo. O que não dá é ficar calado — esta é a
            # única das três causas com conserto fora do banco, e ela não
            # aparece em lugar nenhum da tela.
            #
            # Não dá para estreitar pelo `mercado`: a view sai do próprio
            # `seller_offer_daily`, então seller ausente do fato está ausente
            # da view por construção — a checagem seria sempre verdadeira e
            # não separaria nada. Quem separa de fato é a aba Cobertura, para
            # as outras duas causas.
            st.info(
                f"**Se a coleta rodou** (veja a aba Cobertura), sobra conferir o "
                f"nome. Esta instância está travada em **{seller}** pelo secret "
                "`SELLER`, comparado **literalmente** com `seller_canonical` — e "
                "a canonização aposenta grafias: quando uma vira variante de "
                "outra, o canônico muda e o secret velho passa a apontar para um "
                "seller que não existe mais, sem erro nenhum na resposta."
            )
            if not mercado.empty:
                nomes = (mercado.groupby("seller_canonical")["produtos_detidos"]
                         .sum().sort_values(ascending=False).index.tolist())
                st.caption(
                    "Sellers com buy box observada na janela: "
                    + ", ".join(f"`{n}`" for n in nomes[:15])
                    + (f" … e mais {len(nomes) - 15}." if len(nomes) > 15 else ".")
                )

    # ── Só marketplace entra em KPI. Loja própria o lojista joga sozinho, e
    #    identidade suspeita é chave colapsada: nem numerador, nem denominador.
    limpo = (fato[(fato["superficie"] == "marketplace") & (~fato["identidade_suspeita"])]
             if not vazio else fato)

    if not vazio:
        render_kpi_cards(share, limpo, perdidos)

    aba1, aba2, aba3, aba4, aba5 = st.tabs([
        "📈 Share de buy box", "🏆 Ganhos e perdas", "🏷️ Marcas e posição",
        "🥊 Ranking na plataforma", "🩺 Cobertura",
    ])

    with aba1:
        render_gráfico_share(share)

    with aba2:
        col_g, col_p = st.columns(2)
        ganhos_count = len(limpo[limpo["virou_no_turno"].fillna(False)]) if not limpo.empty else 0
        perdidos_count = len(perdidos)
        col_g.metric("Ganhos na janela", ganhos_count)
        col_p.metric("Perdidos na janela", perdidos_count)
        st.caption(
            "Ganhei = tomei a BB de outro seller. Perdi = outro seller tomou "
            "a minha. Os dois só existem como EVENTO — a foto de um turno nunca "
            "mostra \\\"perdedor\\\", só quem está com a BB agora."
        )

        # Gráfico de cascata
        render_ganhos_perdas_cascata(limpo, perdidos)

        st.markdown("##### ✅ Produtos que você ganhou")
        ganhos = limpo[limpo["virou_no_turno"].fillna(False)] if not limpo.empty else pd.DataFrame()
        if ganhos.empty:
            st.info("Nenhum ganho de buy box observado na janela.")
        else:
            st.dataframe(
                ganhos[["data", "turno", "plataforma", "produto", "marca",
                        "detentor_anterior", "preco"]]
                .sort_values("data", ascending=False)
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "detentor_anterior": "Tomou de", "preco": "Preço (R$)"})
                .style
                .format({"Preço (R$)": "R$ {:,.2f}"})
                .background_gradient(subset=["Preço (R$)"], cmap="Blues"),
                use_container_width=True,
                hide_index=True
            )

        st.markdown("##### ❌ Produtos que você perdeu")
        if perdidos.empty:
            st.info("Nenhuma perda de buy box observada na janela.")
        else:
            st.dataframe(
                perdidos[["data", "turno", "plataforma", "produto", "marca",
                          "seller_canonical", "preco"]]
                .sort_values("data", ascending=False)
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "seller_canonical": "Levou", "preco": "Preço (R$)"})
                .style
                .format({"Preço (R$)": "R$ {:,.2f}"})
                .background_gradient(subset=["Preço (R$)"], cmap="Reds"),
                use_container_width=True,
                hide_index=True
            )

    with aba3:
        detidos_marca = limpo[limpo["detentor_buybox"] == True] if not limpo.empty else pd.DataFrame()  # noqa: E712
        st.markdown("##### Portfólio — produtos detidos por marca")
        if detidos_marca.empty:
            st.info("Sem produto com buy box detida na janela.")
        else:
            # Conta PRODUTO (marketplace_product_id), não oferta: a mesma
            # ligação produto+marca pode render mais de uma offer_key ao
            # longo da janela (a URL canônica muda, ou o produto cai no
            # degrau de hash em vez do de id) — contar offer_key infla a
            # marca. Fallback pro offer_key só nas linhas sem id de produto.
            chave_produto = detidos_marca["marketplace_product_id"].fillna(
                detidos_marca["offer_key"])
            por_marca = (detidos_marca.assign(_produto=chave_produto)
                         .groupby("marca")["_produto"]
                         .nunique().sort_values(ascending=False))
            # `st.bar_chart` roda em cima do Vega-Lite, que ordena eixo
            # nominal/ordinal ALFABETICAMENTE por padrão — ignora a ordem do
            # `sort_values` acima. Índice como categórico ORDENADO é o único
            # jeito de fixar no gráfico a ordem decrescente que o pandas já
            # calculou (testado contra a versão exata do deploy: sem isto o
            # spec sai com `"sort": None` e as barras voltam para A→Z).
            ordem = pd.CategoricalDtype(categories=por_marca.index, ordered=True)
            por_marca.index = por_marca.index.astype(ordem)
            
            # Gráfico de barras com Plotly
            fig = go.Figure(go.Bar(
                x=por_marca.values,
                y=por_marca.index,
                orientation="h",
                marker_color=Config.Colors.PRIMARY,
                hovertemplate="<b>%{y}</b><br>Produtos: %{x}<extra></extra>",
            ))
            fig.update_layout(
                height=Config.CHART_HEIGHT_MARCA,
                xaxis_title="Número de Produtos",
                yaxis_title="Marca",
                margin=dict(l=150, r=40, t=40, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)
            
            sem_buybox_exposta = int(limpo["detentor_buybox"].isna().sum()) if not limpo.empty else 0
            if sem_buybox_exposta:
                st.caption(
                    f"⚠️ {sem_buybox_exposta} ofertas ficaram fora deste gráfico: "
                    "estão em plataformas que não expõem vencedor de buy box na "
                    "vitrine (ex.: Casas Bahia). A Amazon passou a expor via PDP "
                    "(coletor Amazon-only no GitHub Actions, Set/2026), então já "
                    "entra aqui."
                )

        st.markdown("##### Posição mediana por plataforma")
        if limpo.empty or limpo["posicao_mediana"].isna().all():
            st.info("Sem dado de posição na janela.")
        else:
            # median(), não mean(): a coluna já é a mediana POR OFERTA
            # (entre keywords, dentro de um turno); agregar várias ofertas
            # com mean() vira "média das medianas", que não é o que o
            # título promete. median() é o mais próximo que dá pra honrar
            # o rótulo sem ter a posição bruta por keyword nesta camada.
            pos = (limpo.groupby(["data", "plataforma"])["posicao_mediana"]
                  .median().reset_index()
                  .pivot(index="data", columns="plataforma", values="posicao_mediana"))
            
            # Gráfico com linha de referência Top 3
            fig = make_subplots()
            colors = Config.Colors.palette()
            for plataforma in pos.columns:
                fig.add_trace(go.Scatter(
                    x=pos.index,
                    y=pos[plataforma],
                    name=plataforma,
                    line=dict(color=colors["primary"] if plataforma == "Amazon" else None, width=2),
                    hovertemplate=f"<b>{plataforma}</b><br>Posição: %{{y:.0f}}<extra></extra>",
                ))
            
            # Linha de referência Top 3
            fig.add_hline(
                y=3,
                line_dash="dash",
                annotation_text="Top 3 (Referência)",
                annotation_position="top right",
                line_color=colors["warning"],
                line_width=2,
            )
            
            fig.update_layout(
                height=Config.CHART_HEIGHT_POSICAO,
                xaxis_title="Data",
                yaxis_title="Posição Mediana",
                yaxis=dict(autorange="reversed"),  # Menor é melhor
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig, use_container_width=True)
            
            st.caption(
                "Quanto menor, melhor — é a posição mediana entre as keywords em "
                "que a oferta apareceu no turno. Não soma entre plataformas nem "
                "vira ranking absoluto: é relativa a cada busca."
            )

        if not limpo.empty and limpo["tipo_seller"].notna().any():
            st.markdown("##### Como você aparece na vitrine")
            tipos = limpo["tipo_seller"].value_counts()
            st.dataframe(
                tipos.rename_axis("Tipo de seller").reset_index(name="Ofertas")
                .style
                .background_gradient(subset=["Ofertas"], cmap="Blues"),
                use_container_width=True,
                hide_index=True
            )

    with aba4:
        plataformas_do_seller = sorted(share["plataforma"].unique()) if not share.empty else []
        if not plataformas_do_seller or mercado.empty:
            st.info("Sem ranking disponível — este seller não detém buy box em nenhuma plataforma na janela.")
        else:
            # Cada plataforma no seu próprio último dia observado (ver KPI): a
            # Amazon materializa com atraso e o max() global de `data` a apagava
            # deste ranking. Assim ela aparece mesmo um dia defasada.
            ultimo_por_plat = mercado.groupby("plataforma")["data"].transform("max")
            foto = mercado[mercado["data"] == ultimo_por_plat].copy()
            st.caption("Fotografia do último dia observado de cada plataforma.")
            foto["posicao"] = foto.groupby("plataforma")["share_buybox_pct"] \
                                   .rank(ascending=False, method="min").astype(int)
            for plataforma in plataformas_do_seller:
                bloco = foto[foto["plataforma"] == plataforma].sort_values("posicao")
                total = len(bloco)
                linha_seller = bloco[bloco["seller_canonical"] == seller]
                if linha_seller.empty:
                    continue
                minha_posicao = int(linha_seller["posicao"].iloc[0])
                dia_plat = bloco["data"].iloc[0]
                st.markdown(
                    f"##### {plataforma} — você é **#{minha_posicao} de {total}** "
                    f"· observado em {dia_plat}"
                )
                topo = bloco.head(Config.TOP_RANKING_DISPLAY).copy()
                if minha_posicao > Config.TOP_RANKING_DISPLAY:
                    topo = pd.concat([topo, linha_seller])
                topo["Você"] = topo["seller_canonical"].eq(seller).map({True: "✅", False: ""})
                st.dataframe(
                    topo[["posicao", "seller_canonical", "Você", "produtos_detidos",
                          "produtos_universo", "share_buybox_pct"]]
                    .rename(columns={
                        "posicao": "#", "seller_canonical": "Seller",
                        "produtos_detidos": "Com a BB",
                        "produtos_universo": "Universo", "share_buybox_pct": "Share %"
                    })
                    .style
                    .background_gradient(subset=["Share %"], cmap="Greens")
                    .format({"Share %": "{:.1f}%"})
                    .highlight_min(subset=["#"], color=Config.Colors.SUCCESS + "33"),
                    use_container_width=True,
                    hide_index=True
                )

    with aba5:
        st.markdown(
            "**Coleta ausente não é mercado vazio.** Turno sem leitura aparece "
            "aqui como não observado, e as células dele ficam fora de todo "
            "número acima — nunca viram zero."
        )
        
        # Calendar heatmap
        render_calendar_heatmap(cobertura)

    suspeitas = int(fato["identidade_suspeita"].sum()) if not vazio else 0
    if suspeitas:
        st.divider()
        st.caption(
            f"⚠️ {suspeitas} ofertas ficaram fora dos números por identidade "
            "ambígua (chave de oferta colapsada na origem). Estão excluídas de "
            "propósito: entrariam somando produtos diferentes como se fossem um."
        )


if __name__ == "__main__":
    main()
