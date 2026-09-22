# Upstream provenance

- Full browser-agent fork: [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast), base commit `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`, MIT, copyright Browser Use. Fork: [Seomzo/tink-browser-agent](https://github.com/Seomzo/tink-browser-agent).
- Full decision-model fork: [NandhaKishorM/laya](https://github.com/NandhaKishorM/laya), base commit `573e5b62696ba441230cd6be71d593331b5d23af`, Apache-2.0. Fork: [Seomzo/laya](https://github.com/Seomzo/laya). The optional dependency pins the original upstream commit; no Laya source modifications have been made in this phase.
- Model bundle: [convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya), revision `1c5edc17a7acd8701df6fc341c0d179f1c62c982`, model card declares Apache-2.0. Weights are obtained through Hugging Face and are not committed here.
- Browser connection: `browser-harness==0.1.13`, retained from Jev Ultrafast.
- Hosted baseline: TypeSafe `jev-1.13.0`; proprietary model service, with no weights in these repositories.

Tink modifications add optional decision-backend injection, persistent Laya inference, input-loss diagnostics and explicit evaluation tools. Browser mutations, freshness guards, the original text helper and original examples are retained. This repository is an attributed derivative, not an independently trained replacement for the upstream models.
