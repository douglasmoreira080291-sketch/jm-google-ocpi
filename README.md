# JM Google OCPI

Ponte entre a API TriCharge e o feed OCPI 2.2.1 usado para disponibilizar estações e status de conectores.

## Segurança
Nunca coloque `TRICHARGE_CLIENT_SECRET` ou `GOOGLE_OCPI_TOKEN` neste repositório.
Use apenas as variáveis de ambiente da hospedagem.

## Variáveis de ambiente
- `TRICHARGE_CLIENT_ID`
- `TRICHARGE_CLIENT_SECRET`
- `GOOGLE_OCPI_TOKEN`
- `TRICHARGE_BASE_URL` (padrão já configurado)
- `OCPI_COUNTRY_CODE=BR`
- `OCPI_PARTY_ID=JMC`
- `OCPI_OPERATOR_NAME=JM Carregadores`
- `PUBLIC_BASE_URL=https://SEU-SERVICO.onrender.com`

## Endpoint para o Google
`https://SEU-SERVICO.onrender.com/ocpi/cpo/2.2.1/locations`

No portal do Google:
- Dataset URL: endpoint acima
- Token: valor de `GOOGLE_OCPI_TOKEN`
- Pagination: `None`

## Observação importante
Esta primeira versão foi construída a partir da documentação visual da TriCharge.
Antes de enviar ao Google, teste a resposta real da sua estação e confirme endereço,
coordenadas, potência, tipo de conector e status. O conversor foi escrito para tolerar
algumas variações de nomes de campos, mas pode precisar de um pequeno ajuste após o
primeiro teste real.
