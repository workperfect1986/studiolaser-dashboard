# Studio Laser · Dashboard Nucleus

Dashboard operacional com captação de `/work_orders` do Nucleus, filtros locais e módulo de relatório PDF por empresas.

## Funcionalidades

- Importação de trabalhos do Nucleus (todas as páginas)
- Exclusão automática de status **Deletado**
- Contadores recalculados a cada importação
- Filtros locais por **Cliente** e **Operador** (colunas)
- Atualização **manual**
- Módulo de relatório PDF com seleção de empresas

## Requisitos

- Python 3.10+
- Conta Nucleus

## Configuração

```powershell
cd dashboard
python -m pip install -r requirements.txt

$env:NUCLEUS_BASE_URL = "https://studiolaser.nucleusapp.com.br"
$env:NUCLEUS_EMAIL = "seu@email.com"
$env:NUCLEUS_PASSWORD = "sua-senha"
$env:NUCLEUS_OPERADOR_ID = "7012"
$env:PORT = "8765"
```

Ou use `start.ps1` após definir as variáveis de ambiente.

## Executar

```powershell
cd dashboard
python server.py
```

Abra:

- Dashboard: http://127.0.0.1:8765/
- Relatório PDF: http://127.0.0.1:8765/relatorio
- Propostas de design: http://127.0.0.1:8765/designs/

## Segurança

Não commite e-mail/senha. Use variáveis de ambiente (`NUCLEUS_EMAIL`, `NUCLEUS_PASSWORD`).
