"""
Radar Tributário: robô de abastecimento do painel.
Baixa a base aberta do CNPJ (Receita Federal), filtra as cidades escolhidas,
aplica as teses e a pontuação e manda um CSV pronto para importar no painel,
pelo Telegram.
"""
import csv
import io
import os
import re
import sys
import time
import zipfile
from datetime import date
from xml.etree import ElementTree

import requests

# ================== CONFIGURAÇÃO (pode editar) ==================
CIDADES = ["VOTUPORANGA"]          # nomes como a Receita escreve, sem acento
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

SHARE_TOKEN = "YggdBLfdninEJX9"
DAV = "https://arquivos.receitafederal.gov.br/public.php/webdav/"
FILES = "https://arquivos.receitafederal.gov.br/public.php/dav/files/%s/%s/%s"
PASTA = "dados"
HEADERS = {"User-Agent": "Mozilla/5.0 (radar-tributario)"}
PONTOS_FORCA = {"A": 40, "M": 25, "B": 10, "R": 10}
_NOMES = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


ETAPA = "início"
ARQUIVOS_PADRAO = (["Cnaes.zip", "Municipios.zip", "Simples.zip"]
                   + [f"{p}{i}.zip" for p in ("Empresas", "Estabelecimentos", "Socios") for i in range(10)])


def propfind(url):
    for tentativa in range(6):
        try:
            r = requests.request("PROPFIND", url, auth=(SHARE_TOKEN, ""),
                                 headers={"Depth": "1", **HEADERS}, timeout=90)
            r.raise_for_status()
            return r
        except Exception as e:
            log(f"  listagem falhou ({e}); tentativa {tentativa + 1}")
            time.sleep(20 * (tentativa + 1))
    raise RuntimeError("a Receita não respondeu à listagem de arquivos")


def mes_sem_listagem():
    """Plano B: tenta o mês atual e os dois anteriores, sem listar a pasta."""
    hoje = date.today()
    for volta in range(3):
        a, m = hoje.year, hoje.month - volta
        while m <= 0:
            a, m = a - 1, m + 12
        mes = f"{a}-{m:02d}"
        try:
            r = requests.get(FILES % (SHARE_TOKEN, mes, "Cnaes.zip"), headers=HEADERS,
                             stream=True, timeout=(30, 90))
            if r.ok:
                r.close()
                return mes, list(ARQUIVOS_PADRAO)
        except Exception as e:
            log(f"  teste do mês {mes} falhou ({e})")
    raise RuntimeError("não encontrei a pasta do mês na Receita")


def ultimo_mes_e_arquivos():
    try:
        return _ultimo_mes_e_arquivos()
    except Exception as e:
        log("Listagem falhou, usando plano B:", e)
        return mes_sem_listagem()


def _ultimo_mes_e_arquivos():
    ns = {"d": "DAV:"}
    r = propfind(DAV)
    meses = sorted(re.search(r"(\d{4}-\d{2})/?$", e.find("d:href", ns).text).group(1)
                   for e in ElementTree.fromstring(r.content).findall("d:response", ns)
                   if re.search(r"(\d{4}-\d{2})/?$", e.find("d:href", ns).text))
    mes = meses[-1]
    r = propfind(DAV + mes + "/")
    arqs = [re.search(r"/([^/]+\.zip)$", e.find("d:href", ns).text, re.I).group(1)
            for e in ElementTree.fromstring(r.content).findall("d:response", ns)
            if re.search(r"/([^/]+\.zip)$", e.find("d:href", ns).text, re.I)]
    return mes, arqs


def baixar(mes, nome):
    global ETAPA
    ETAPA = f"download de {nome}"
    os.makedirs(PASTA, exist_ok=True)
    destino = os.path.join(PASTA, nome)
    url = FILES % (SHARE_TOKEN, mes, nome)
    for tentativa in range(8):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=(30, 300)) as r:
                r.raise_for_status()
                with open(destino, "wb") as f:
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
            zipfile.ZipFile(destino).testzip()
            return destino
        except Exception as e:
            log(f"  falha ao baixar {nome} ({e}); tentando de novo")
            time.sleep(min(30 * (tentativa + 1), 180))
    raise RuntimeError(f"Não consegui baixar {nome}")


def linhas(caminho):
    """Lê o CSV (latin-1, ';') de dentro do zip, linha a linha."""
    with zipfile.ZipFile(caminho) as z:
        with z.open(z.namelist()[0]) as f:
            texto = io.TextIOWrapper(f, encoding="latin-1", newline="")
            yield from csv.reader(texto, delimiter=";", quotechar='"')


def processar(mes, arqs, baixar_fn=baixar):
    grupo = lambda prefixo: sorted(a for a in arqs if a.lower().startswith(prefixo))

    # 1. Códigos de município e descrições de CNAE
    cod_cidades = set()
    for l in linhas(baixar_fn(mes, grupo("municipios")[0])):
        if len(l) >= 2 and l[1].strip().upper() in CIDADES:
            cod_cidades.add(l[0].strip())
            _NOMES[l[0].strip()] = l[1].strip().title()
    cnaes = {l[0].strip(): l[1].strip() for l in linhas(baixar_fn(mes, grupo("cnaes")[0])) if len(l) >= 2}
    log("Municípios encontrados:", cod_cidades)
    if not cod_cidades:
        raise RuntimeError("Nenhuma cidade encontrada. Confira os nomes em CIDADES.")

    # 2. Estabelecimentos ativos das cidades
    estab = {}
    for nome in grupo("estabelecimentos"):
        caminho = baixar_fn(mes, nome)
        for l in linhas(caminho):
            if len(l) < 28 or l[19] != UF or l[20] not in cod_cidades or l[5] != "02":
                continue
            estab[l[0] + l[1] + l[2]] = l
        os.remove(caminho)
        log(f"{nome}: {len(estab)} estabelecimentos ativos até agora")
    basicos = {k[:8] for k in estab}

    # 3. Empresas (razão social, capital, porte)
    empresas = {}
    for nome in grupo("empresas"):
        caminho = baixar_fn(mes, nome)
        for l in linhas(caminho):
            if len(l) >= 6 and l[0] in basicos:
                empresas[l[0]] = l
        os.remove(caminho)
    log("Empresas lidas:", len(empresas))

    # 4. Simples / MEI
    simples = {}
    caminho = baixar_fn(mes, grupo("simples")[0])
    for l in linhas(caminho):
        if len(l) >= 5 and l[0] in basicos:
            simples[l[0]] = (l[1] == "S", l[4] == "S")
    os.remove(caminho)

    # 5. Sócios (primeiro sócio pessoa física/administrador encontrado)
    socios = {}
    if BAIXAR_SOCIOS:
        for nome in grupo("socios"):
            caminho = baixar_fn(mes, nome)
            for l in linhas(caminho):
                if len(l) >= 3 and l[0] in basicos and l[0] not in socios and l[2].strip():
                    socios[l[0]] = l[2].strip().title()
            os.remove(caminho)
    return estab, empresas, simples, socios, cnaes


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
                capital = float((emp[4] if len(emp) > 4 else "0").replace(".", "").replace(",", "."))
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
    avisar("⏳ Radar Tributário: comecei a baixar a base da Receita. Pode levar até 3 horas.")
    ETAPA = "listagem de arquivos da Receita"
    mes, arqs = ultimo_mes_e_arquivos()
    log("Base da Receita:", mes, "com", len(arqs), "arquivos")
    estab, empresas, simples, socios, cnaes = processar(mes, arqs)
    ETAPA = "montagem da planilha"
    saida = f"empresas-{mes}.csv"
    total = montar_csv(estab, empresas, simples, socios, cnaes, saida)
    log("Empresas no CSV:", total)
    enviar_telegram(saida, f"📊 Radar Tributário · base {mes} · {total} empresas em "
                           f"{', '.join(c.title() for c in CIDADES)}. Importe este arquivo no painel.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        avisar_erro(f"parou na etapa \"{ETAPA}\": {e}")
        raise
