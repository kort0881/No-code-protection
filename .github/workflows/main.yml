name: Update Script

on:
  schedule:
    - cron: '0 0 * * *'
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Make script executable
        run: chmod +x scripts/update.sh
      - name: Run update script
        run: ./scripts/update.sh
