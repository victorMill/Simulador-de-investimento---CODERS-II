import pandas as pd
import requests
from pandas.tseries.offsets import BDay

# === 1. Carregar e corrigir os dados
df_rf = pd.read_excel("RF_RegisterPlan.xlsx")

# Corrigir valores com separadores brasileiros
df_rf['VALOR'] = df_rf['VALOR'].astype(str).str.replace(',', '.', regex=False).astype(float)

# Corrigir taxas como "110%" → 1.10
df_rf['TAXA'] = df_rf['TAXA'].astype(str).str.replace('%', '', regex=False).str.replace(',', '.', regex=False)
df_rf['TAXA'] = pd.to_numeric(df_rf['TAXA'], errors='coerce')

# Converter datas
df_rf['DATA APLICACAO'] = pd.to_datetime(df_rf['DATA APLICACAO'])
df_rf['DATA VENCIMENTO'] = pd.to_datetime(df_rf['DATA VENCIMENTO'], errors='coerce')

# Filtrar apenas aplicações EM SER com benchmark CDI ou PRE
df_filtrado = df_rf[
    df_rf['BENCHMARK'].isin(['CDI', 'PRE']) & (df_rf['SIT'] == 'EM SER')
].copy()

# === 2. Baixar série histórica CDI
data_inicial_serie = df_filtrado['DATA APLICACAO'].min().strftime('%d/%m/%Y')
data_final_serie = (pd.Timestamp.today() - BDay(1)).strftime('%d/%m/%Y')

url = f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.4389/dados?formato=json&dataInicial={data_inicial_serie}&dataFinal={data_final_serie}'
response = requests.get(url)
data = response.json()

df_cdi = pd.DataFrame(data)
df_cdi['data'] = pd.to_datetime(df_cdi['data'], format='%d/%m/%Y')
df_cdi['valor'] = df_cdi['valor'].astype(float)
df_cdi = df_cdi.sort_values('data').reset_index(drop=True)

# === 3. Calcular FTMCDI base
df_cdi['FTMCDI'] = 1.0
for i in range(1, len(df_cdi)):
    df_cdi.loc[i, 'FTMCDI'] = (
        df_cdi.loc[i - 1, 'FTMCDI']
        * (1 + df_cdi.loc[i - 1, 'valor'] / 100) ** (1 / 252)
    )

# Última taxa CDI disponível
ultima_taxa_cdi = df_cdi['valor'].iloc[-1]

# === 4. Calcular saldos e montar tabela de resultados
hoje = pd.Timestamp.today().normalize()
dados_resultado = []

for idx, row in df_filtrado.iterrows():
    data_aplic = row['DATA APLICACAO']
    data_venc = row['DATA VENCIMENTO']
    valor_ini = row['VALOR']
    fator_taxa = row['TAXA']
    benchmark = row['BENCHMARK']
    titulo = row.get('TITULO', 'N/A')
    emissor = row.get('EMISSOR', 'N/A')
    tipo = str(titulo).strip().upper()

    # === Cálculo saldo bruto até hoje
    if benchmark == 'CDI':
        df_temp = df_cdi[df_cdi['data'] >= data_aplic].copy().reset_index(drop=True)

        if df_temp.empty:
            saldo_hoje = valor_ini
        else:
            df_temp['FTMCDI_ajustado'] = 1.0
            for i in range(1, len(df_temp)):
                taxa_ajustada = df_temp.loc[i - 1, 'valor'] * fator_taxa
                df_temp.loc[i, 'FTMCDI_ajustado'] = (
                    df_temp.loc[i - 1, 'FTMCDI_ajustado']
                    * (1 + taxa_ajustada / 100) ** (1 / 252)
                )
            saldo_hoje = round(valor_ini * df_temp.iloc[-1]['FTMCDI_ajustado'], 2)

    elif benchmark == 'PRE':
        taxa_dia = (1 + fator_taxa) ** (1 / 252) - 1
        dias_uteis = df_cdi[df_cdi['data'] >= data_aplic]['data']
        num_dias_uteis = len(dias_uteis)
        saldo_hoje = round(valor_ini * (1 + taxa_dia) ** num_dias_uteis, 2)

    else:
        saldo_hoje = valor_ini

    # === Cálculo IR até hoje
    dias_corridos = (hoje - data_aplic.normalize()).days

    if tipo == 'CDB':
        if dias_corridos <= 180:
            aliquota_ir = 22.5
        elif dias_corridos <= 360:
            aliquota_ir = 20.0
        elif dias_corridos <= 720:
            aliquota_ir = 17.5
        else:
            aliquota_ir = 15.0

        ir_hoje = round((saldo_hoje - valor_ini) * aliquota_ir / 100, 2)
    else:
        aliquota_ir = 0.0
        ir_hoje = 0.0

    saldo_liquido_hoje = saldo_hoje - ir_hoje

    # === Simulação até vencimento (líquido de IR)
    if pd.notnull(data_venc) and data_venc > hoje:
        dias_uteis_sim = pd.date_range(
            start=hoje + BDay(1),
            end=data_venc,
            freq=BDay()
        ).size

        if benchmark == 'CDI':
            taxa_simulada = (1 + (ultima_taxa_cdi * fator_taxa / 100)) ** (1 / 252) - 1
        elif benchmark == 'PRE':
            taxa_simulada = (1 + fator_taxa) ** (1 / 252) - 1
        else:
            taxa_simulada = 0.0

        saldo_simulado = round(saldo_hoje * (1 + taxa_simulada) ** dias_uteis_sim, 2)

        # Cálculo IR simulado
        dias_totais_sim = (data_venc - data_aplic).days

        if tipo == 'CDB':
            if dias_totais_sim <= 180:
                aliquota_sim = 22.5
            elif dias_totais_sim <= 360:
                aliquota_sim = 20.0
            elif dias_totais_sim <= 720:
                aliquota_sim = 17.5
            else:
                aliquota_sim = 15.0

            ir_sim = round((saldo_simulado - valor_ini) * aliquota_sim / 100, 2)
        else:
            aliquota_sim = 0.0
            ir_sim = 0.0

        saldo_liquido_sim = saldo_simulado - ir_sim
    else:
        saldo_liquido_sim = ''

    # === Montar resultado
    dados_resultado.append({
        'Data Aplicação': data_aplic.date(),
        'Título': titulo,
        'Emissor': emissor,
        'Tipo': tipo,
        'Benchmark': benchmark,
        'Valor Inicial (R$)': f"R$ {valor_ini:,.2f}",
        'Saldo Bruto (R$)': f"R$ {saldo_hoje:,.2f}",
        'Alíquota IR (%)': round(aliquota_ir, 2),
        'IR (R$)': f"R$ {ir_hoje:,.2f}",
        'Saldo Líquido (R$)': f"R$ {saldo_liquido_hoje:,.2f}",
        'Sim. Líquido até Venc. (R$)': f"R$ {saldo_liquido_sim:,.2f}" if saldo_liquido_sim != '' else ''
    })

# === Finalizar DataFrame e total
df_result = pd.DataFrame(dados_resultado)

def moeda_str_para_float(valor_str):
    if valor_str == '' or pd.isna(valor_str):
        return 0.0
    return float(valor_str.replace('R$ ', '').replace(',', ''))

df_result['Valor Inicial (Float)'] = df_result['Valor Inicial (R$)'].apply(moeda_str_para_float)
df_result['Saldo Líquido (Float)'] = df_result['Saldo Líquido (R$)'].apply(moeda_str_para_float)
df_result['Sim. Líquido até Venc. (Float)'] = df_result['Sim. Líquido até Venc. (R$)'].apply(moeda_str_para_float)

# === Totais
total_valor_inicial = df_result['Valor Inicial (Float)'].sum()
total_saldo_liquido = df_result['Saldo Líquido (Float)'].sum()
total_sim_liquido = df_result['Sim. Líquido até Venc. (Float)'].sum()

linha_total = {
    'Data Aplicação': '',
    'Título': '',
    'Emissor': '',
    'Tipo': '',
    'Benchmark': '',
    'Valor Inicial (R$)': '',
    'Saldo Bruto (R$)': '',
    'Alíquota IR (%)': '',
    'IR (R$)': 'TOTAL:',
    'Saldo Líquido (R$)': f"R$ {total_saldo_liquido:,.2f}",
    'Sim. Líquido até Venc. (R$)': f"R$ {total_sim_liquido:,.2f}",
}

df_result = pd.concat([df_result, pd.DataFrame([linha_total])], ignore_index=True)

df_result.drop(
    columns=[
        'Valor Inicial (Float)',
        'Saldo Líquido (Float)',
        'Sim. Líquido até Venc. (Float)'
    ],
    inplace=True
)

df_result['Alíquota IR (%)'] = pd.to_numeric(df_result['Alíquota IR (%)'], errors='coerce')

print(df_result.to_string(formatters={
    'Alíquota IR (%)': lambda x: f"{x:.2f}" if pd.notna(x) else ''
}))
# === 5. Resumo por prazo de vencimento
hoje = pd.Timestamp.today().normalize()

# Criar uma cópia apenas das aplicações em ser com vencimento válido
df_validos = df_filtrado.copy()
df_validos = df_validos[pd.notnull(df_validos['DATA VENCIMENTO'])].copy()

# Função segura para conversão
def moeda_str_para_float(x):
    if x in [None, '', 'nan']:
        return 0.0
    return float(str(x).replace('R$ ', '').replace(',', ''))

# Recalcular valores numéricos (garantindo alinhamento de índice)
valores_sim_liq = df_result.loc[
    df_result['Data Aplicação'] != '',
    'Sim. Líquido até Venc. (R$)'
].apply(moeda_str_para_float).values

df_validos = df_validos.reset_index(drop=True)
df_validos['Sim. Líquido até Venc. (Float)'] = valores_sim_liq

# Calcular dias até vencimento
df_validos['dias_para_vencer'] = (df_validos['DATA VENCIMENTO'] - hoje).dt.days

# Classificação
def classificar_vencimento(dias):
    if dias <= 7:
        return "Até 7 dias"
    elif dias <= 30:
        return "Até 1 mês"
    elif dias <= 180:
        return "Até 6 meses"
    elif dias <= 365:
        return "Até 12 meses"
    else:
        return "Acima de 12 meses"

df_validos['faixa_vencimento'] = df_validos['dias_para_vencer'].apply(classificar_vencimento)

# Ordem fixa
ordem_faixas = [
    "Até 7 dias",
    "Até 1 mês",
    "Até 6 meses",
    "Até 12 meses",
    "Acima de 12 meses"
]

df_validos['faixa_vencimento'] = pd.Categorical(
    df_validos['faixa_vencimento'],
    categories=ordem_faixas,
    ordered=True
)

# Agrupamento
resumo_vencimentos = df_validos.groupby('faixa_vencimento').agg(
    Quantidade=('Sim. Líquido até Venc. (Float)', 'count'),
    Saldo_Liquido_Total=('Sim. Líquido até Venc. (Float)', 'sum')
).reindex(ordem_faixas).reset_index()

# Substituir NaN
resumo_vencimentos = resumo_vencimentos.fillna(0)

# Linha TOTAL
linha_total_venc = pd.DataFrame([{
    'faixa_vencimento': 'TOTAL',
    'Quantidade': resumo_vencimentos['Quantidade'].sum(),
    'Saldo_Liquido_Total': resumo_vencimentos['Saldo_Liquido_Total'].sum()
}])

resumo_vencimentos = pd.concat([resumo_vencimentos, linha_total_venc], ignore_index=True)

# Exibir
print("\n=== Resumo de Vencimentos ===")
print(resumo_vencimentos.to_string(index=False, formatters={
    'Saldo_Liquido_Total': lambda x: f"R$ {x:,.2f}"
}))
