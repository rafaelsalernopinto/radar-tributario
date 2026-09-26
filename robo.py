"""
Radar Tributário: robô de abastecimento do painel.
Consulta a base aberta do CNPJ (Receita Federal) na cópia pública da Base dos Dados
no Google BigQuery, filtra as cidades escolhidas,
aplica as teses e a pontuação e manda um CSV pronto para importar no painel,
pelo Telegram.
"""
import csv
import json
import os
import time
from datetime import date

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

# ================== CONFIGURAÇÃO (pode editar) ==================
CIDADES = ["Votuporanga"]          # nomes das cidades, como no IBGE
UF = "SP"
IDADE_MINIMA_ANOS = 2
BAIXAR_SOCIOS = True               # False deixa o robô mais rápido, sem nome do sócio

# Força por CNAE em cada tese: A = Alta, M = Média, B = Baixa, R = revisão manual
TESE1_ICMS_ST = {  # fora do Simples
    "4744099": "A", "4744005": "A", "4744001": "A", "4744003": "A", "4742300": "A",
    "4741500": "A", "4679699": "A", "4672900": "A", "4673700": "A", "4711302": "A",
    "4711301": "A", "4712100": "M", "4639701": "M", "4649408": "M", "4753900": "M",
    "4789004": "M", "4771701": "M", "4530703": "M", "4530701": "M", "4635499": "M",
    "4723700": "B", "4772500": "B", "4731800": "B",
}
TESE2_SIMPLES = {  # optante do Simples, sem MEI
    "4771701": "A", "4772500": "A", "4530703": "A", "4530705": "A", "4530701": "A",
    "4530702": "A", "4723700": "A", "4635402": "A", "4635401": "A", "4711302": "M",
    "4712100": "M", "4729602": "M", "4731800": "M", "4789004": "M", "5611204": "M",
    "1091102": "M", "4744099": "M", "4744001": "M", "4742300": "M", "5611201": "B",
    "4771702": "B",
}
TESE3_SERVICOS_MANUAL = {"4930202", "4930201", "8610101", "8640202", "8121400", "8011101", "4120400"}
TESE4_ISS = {  # fora do Simples; tese aguardando STF
    "6201501": "M", "6204000": "M", "6920601": "M", "6911701": "M", "7112000": "M",
    "7020400": "M", "8610101": "M", "8630503": "M", "8640202": "M", "8513900": "M",
    "7311400": "M", "8121400": "M", "8011101": "M", "7820500": "M", "4520001": "B",
}
# ================================================================

PONTOS_FORCA = {"A": 40, "M": 25, "B": 10, "R": 10}
_NOMES = {}
ETAPA = "início"
DS = "basedosdados.br_me_cnpj"
LIMITE_BYTES = 300 * 10**9   # trava de segurança por consulta (bem abaixo do 1 TB grátis)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cliente():
    info = json.loads(os.environ["GCP_KEY"])
    cred = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(credentials=cred, project=info["project_id"])


def colunas(bq, tabela):
    return [c.name for c in bq.get_table(tabela).schema]


def escolher(cols, candidatos, tabela, obrigatoria=True):
    for c in candidatos:
        if c in cols:
            return c
    if obrigatoria:
        raise RuntimeError(f"coluna {candidatos[0]} não existe em {tabela}. Colunas: {', '.join(cols)}")
    return None


def consulta(bq, sql, params=()):
    cfg = bigquery.QueryJobConfig(query_parameters=list(params), maximum_bytes_billed=LIMITE_BYTES)
    return list(bq.query(sql, job_config=cfg).result())


def ultimo_periodo(bq, tabela, cols):
    if "ano" in cols and "mes" in cols:
        r = consulta(bq, f"SELECT ano, mes FROM `{tabela}` GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC LIMIT 1")
        return f"ano = {int(r[0].ano)} AND mes = {int(r[0].mes)}", f"{int(r[0].ano)}-{int(r[0].mes):02d}"
    if "data" in cols:
        r = consulta(bq, f"SELECT MAX(data) AS d FROM `{tabela}`")
        return f"data = '{r[0].d}'", str(r[0].d)[:7]
    return "TRUE", "atual"


def txt(v):
    return "" if v is None else str(v).strip()


def sim(v):
    return txt(v).upper() in ("S", "SIM", "1", "TRUE", "T")


def zeros(v):
    return txt(v).lstrip("0")


def buscar():
    global ETAPA
    bq = cliente()

    ETAPA = "códigos das cidades"
    mun = consulta(bq, """SELECT id_municipio, nome FROM `basedosdados.br_bd_diretorios_brasil.municipio`
                          WHERE sigla_uf = @uf AND nome IN UNNEST(@nomes)""",
                   [bigquery.ScalarQueryParameter("uf", "STRING", UF),
                    bigquery.ArrayQueryParameter("nomes", "STRING", CIDADES)])
    ids = [txt(r.id_municipio) for r in mun]
    for r in mun:
        _NOMES[txt(r.id_municipio)] = txt(r.nome)
    if not ids:
        raise RuntimeError("nenhuma cidade encontrada; confira os nomes em CIDADES")
    log("Cidades:", _NOMES)

    ETAPA = "estabelecimentos"
    t = f"{DS}.estabelecimentos"
    c = colunas(bq, t)
    filtro, periodo = ultimo_periodo(bq, t, c)
    campos = {k: escolher(c, v, t, obr) for k, v, obr in [
        ("cnpj", ["cnpj"], True), ("fantasia", ["nome_fantasia"], False),
        ("situacao", ["situacao_cadastral"], True), ("inicio", ["data_inicio_atividade"], True),
        ("cnae", ["cnae_fiscal_principal", "cnae_principal"], True), ("tipo", ["tipo_logradouro"], False),
        ("logradouro", ["logradouro"], False), ("numero", ["numero"], False), ("bairro", ["bairro"], False),
        ("cep", ["cep"], False), ("ddd1", ["ddd_1", "ddd1"], False), ("tel1", ["telefone_1", "telefone1"], False),
        ("ddd2", ["ddd_2", "ddd2"], False), ("tel2", ["telefone_2", "telefone2"], False),
        ("email", ["email"], False), ("municipio", ["id_municipio"], True), ("uf", ["sigla_uf"], True)]}
    sel = ", ".join(f"{v} AS {k}" for k, v in campos.items() if v)
    linhas = consulta(bq, f"SELECT {sel} FROM `{t}` WHERE {filtro} AND {campos['uf']} = @uf "
                          f"AND CAST({campos['municipio']} AS STRING) IN UNNEST(@ids)",
                      [bigquery.ScalarQueryParameter("uf", "STRING", UF),
                       bigquery.ArrayQueryParameter("ids", "STRING", ids)])
    estab = {}
    for r in linhas:
        g = lambda k: txt(getattr(r, k, None)) if campos.get(k) else ""
        situ = g("situacao").upper()
        if zeros(situ) != "2" and "ATIVA" not in situ:
            continue
        cnpj = "".join(ch for ch in g("cnpj") if ch.isdigit()).zfill(14)
        l = [""] * 30
        l[0], l[1], l[2] = cnpj[:8], cnpj[8:12], cnpj[12:]
        l[4] = g("fantasia")
        l[10] = g("inicio").replace("-", "")[:8]
        l[11] = "".join(ch for ch in g("cnae") if ch.isdigit()).zfill(7)
        l[13], l[14], l[15], l[17], l[18] = g("tipo"), g("logradouro"), g("numero"), g("bairro"), g("cep")
        l[19], l[20] = UF, g("municipio")
        l[21], l[22], l[23], l[24], l[27] = g("ddd1"), g("tel1"), g("ddd2"), g("tel2"), g("email")
        estab[cnpj] = l
    basicos = sorted({k[:8] for k in estab})
    log("Estabelecimentos ativos:", len(estab), "período", periodo)
    if not basicos:
        raise RuntimeError(f"nenhuma empresa ativa encontrada (período {periodo})")
    p_b = bigquery.ArrayQueryParameter("b", "STRING", basicos)

    ETAPA = "empresas"
    t = f"{DS}.empresas"
    c = colunas(bq, t)
    filtro, _ = ultimo_periodo(bq, t, c)
    cr = escolher(c, ["razao_social"], t)
    cc = escolher(c, ["capital_social"], t)
    cp = escolher(c, ["porte"], t)
    empresas = {}
    for r in consulta(bq, f"SELECT cnpj_basico, {cr} AS razao, {cc} AS capital, {cp} AS porte FROM `{t}` "
                          f"WHERE {filtro} AND cnpj_basico IN UNNEST(@b)", [p_b]):
        porte = txt(r.porte).upper()
        porte = {"1": "01", "3": "03", "5": "05"}.get(zeros(porte), porte)
        if "MICRO" in porte:
            porte = "01"
        elif "PEQUENO" in porte:
            porte = "03"
        elif "DEMAIS" in porte:
            porte = "05"
        cap = txt(r.capital).replace(",", ".") or "0"
        empresas[txt(r.cnpj_basico)] = [txt(r.cnpj_basico), txt(r.razao), "", "", cap, porte]

    ETAPA = "simples"
    t = f"{DS}.simples"
    c = colunas(bq, t)
    filtro, _ = ultimo_periodo(bq, t, c)
    cs = escolher(c, ["opcao_simples"], t)
    cm = escolher(c, ["opcao_mei"], t)
    simples = {txt(r.cnpj_basico): (sim(r.s), sim(r.m)) for r in consulta(
        bq, f"SELECT cnpj_basico, {cs} AS s, {cm} AS m FROM `{t}` WHERE {filtro} AND cnpj_basico IN UNNEST(@b)", [p_b])}

    socios = {}
    if BAIXAR_SOCIOS:
        ETAPA = "sócios"
        try:
            t = f"{DS}.socios"
            c = colunas(bq, t)
            filtro, _ = ultimo_periodo(bq, t, c)
            cn = escolher(c, ["nome", "nome_socio"], t)
            for r in consulta(bq, f"SELECT cnpj_basico, {cn} AS nome FROM `{t}` "
                                  f"WHERE {filtro} AND cnpj_basico IN UNNEST(@b)", [p_b]):
                socios.setdefault(txt(r.cnpj_basico), txt(r.nome).title())
        except Exception as e:
            log("Sócios ficaram de fora:", e)

    cnaes = {}
    try:
        for r in consulta(bq, "SELECT subclasse, descricao_subclasse FROM `basedosdados.br_bd_diretorios_brasil.cnae_2`"):
            cnaes["".join(ch for ch in txt(r.subclasse) if ch.isdigit())] = txt(r.descricao_subclasse)
    except Exception as e:
        log("Descrições de CNAE ficaram de fora:", e)
    return periodo, estab, empresas, simples, socios, cnaes


def idade(data_inicio):
    try:
        d = date(int(data_inicio[:4]), int(data_inicio[4:6]), int(data_inicio[6:8]))
        return (date.today() - d).days / 365.25
    except Exception:
        return 0


def classificar(cnae, e_simples, e_mei, porte, capital):
    """Devolve lista de (tese, força, limita_a_baixa)."""
    teses = []
    if e_mei:
        return teses
    if e_simples:
        if cnae in TESE2_SIMPLES:
            teses.append(("Monofásico/ST Simples", TESE2_SIMPLES[cnae], False))
        return teses
    if cnae in TESE1_ICMS_ST:
        teses.append(("ICMS-ST", TESE1_ICMS_ST[cnae], False))
    divisao = int(cnae[:2]) if cnae[:2].isdigit() else 0
    sinal_real = porte == "05" or capital >= 1_000_000
    if 10 <= divisao <= 33 and sinal_real:
        teses.append(("Insumos", "A", False))
    elif cnae in TESE3_SERVICOS_MANUAL:
        teses.append(("Insumos (revisão manual)", "R", True))
    if cnae in TESE4_ISS:
        teses.append(("ISS (aguardando STF)", TESE4_ISS[cnae], True))
    return teses


def pontuar(teses, anos, capital, porte):
    p = max(PONTOS_FORCA[f] for _, f, _ in teses)
    p += 25 if anos >= 5 else 15 if anos >= 3 else 5
    p += 20 if capital >= 1_000_000 else 12 if capital >= 200_000 else 5
    p += {"05": 10, "03": 7, "01": 4}.get(porte, 4)
    if len(teses) > 1:
        p += 5
    p = min(p, 100)
    prioridade = "Alta" if p >= 60 else "Média" if p >= 35 else "Baixa"
    if all(limita for _, _, limita in teses):
        prioridade = "Baixa"
    return p, prioridade


def montar_csv(estab, empresas, simples, socios, cnaes, saida):
    campos = ["cnpj", "razao_social", "nome_fantasia", "municipio", "uf", "bairro", "logradouro",
              "numero", "cep", "telefone1", "telefone2", "email", "cnae_principal", "cnae_descricao",
              "data_inicio", "capital_social", "socio_principal", "teses", "prioridade", "pontuacao"]
    total = 0
    with open(saida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for cnpj, l in estab.items():
            emp = empresas.get(cnpj[:8], [])
            porte = emp[5] if len(emp) > 5 else ""
            try:
                capital = float(emp[4] if len(emp) > 4 else "0")
            except ValueError:
                capital = 0.0
            e_simples, e_mei = simples.get(cnpj[:8], (False, False))
            anos = idade(l[10])
            if anos < IDADE_MINIMA_ANOS:
                continue
            teses = classificar(l[11], e_simples, e_mei, porte, capital)
            if not teses:
                continue
            pontos, prioridade = pontuar(teses, anos, capital, porte)
            fone = lambda ddd, tel: (ddd.strip() + tel.strip()) if tel.strip() else ""
            w.writerow({
                "cnpj": cnpj, "razao_social": emp[1] if len(emp) > 1 else "",
                "nome_fantasia": l[4].strip(), "municipio": nome_cidade(l[20]), "uf": l[19],
                "bairro": l[17].strip().title(), "logradouro": (l[13] + " " + l[14]).strip().title(),
                "numero": l[15].strip(), "cep": l[18].strip(),
                "telefone1": fone(l[21], l[22]), "telefone2": fone(l[23], l[24]),
                "email": l[27].strip().lower(), "cnae_principal": l[11],
                "cnae_descricao": cnaes.get(l[11], ""),
                "data_inicio": f"{l[10][:4]}-{l[10][4:6]}-{l[10][6:8]}" if len(l[10]) == 8 else "",
                "capital_social": f"{capital:.2f}", "socio_principal": socios.get(cnpj[:8], ""),
                "teses": "|".join(t for t, _, _ in teses), "prioridade": prioridade, "pontuacao": pontos,
            })
            total += 1
    return total


def nome_cidade(cod):
    return _NOMES.get(cod, cod)


def enviar_telegram(caminho, legenda):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    with open(caminho, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                          data={"chat_id": chat, "caption": legenda},
                          files={"document": (os.path.basename(caminho), f, "text/csv")}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Erro do Telegram: {r.text}")


def avisar_erro(msg):
    try:
        token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": "⚠️ Radar Tributário: " + msg[:3500]}, timeout=30)
    except Exception:
        pass


def avisar(msg):
    try:
        token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=30)
    except Exception:
        pass


def main():
    global ETAPA
    periodo, estab, empresas, simples, socios, cnaes = buscar()
    ETAPA = "montagem da planilha"
    saida = f"empresas-{periodo}.csv"
    total = montar_csv(estab, empresas, simples, socios, cnaes, saida)
    log("Empresas no CSV:", total)
    enviar_telegram(saida, f"📊 Radar Tributário · base {periodo} · {total} empresas em "
                           f"{', '.join(CIDADES)}. Importe este arquivo no painel.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        avisar(f"⚠️ Radar Tributário: parou na etapa \"{ETAPA}\": {str(e)[:3000]}")
        raise

