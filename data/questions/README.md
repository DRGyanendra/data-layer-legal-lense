# Question sets

`indiclegalqa.json` — IndicLegalQA (Revised), 10,000 question–answer pairs over 1,260 Supreme
Court judgments, 2015–2024. National Institute of Technology Srinagar, Mendeley Data,
doi:10.17632/gf8n8cnmvc.2, licence CC BY 4.0. Redistributed here unchanged, with attribution.

`python -m legal_lens questions data/questions/indiclegalqa.json` matches its cases to the AWS Open
Data bucket (983 of 1,260 by decision date and party names) and writes `data/eval.indiclegalqa.yaml`
for `evaluate`. The set records no paragraph numbers, so a hit there means a chunk from the right
case; paragraph-level citation is measured with the hand-written `data/eval.yaml` instead.
