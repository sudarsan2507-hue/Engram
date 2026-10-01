# Deploy Engram as a public Hugging Face demo

Engram is packaged as a Hugging Face Docker Space. The root `README.md` already contains the Space metadata and the `Dockerfile` starts the single-port gateway on port 7860.

## Publish

1. Create a new **public** Space on Hugging Face and select the **Docker** SDK.
2. Clone the Space repository, then copy this repository's tracked files into it (or add the Space as a second Git remote and push `main`).
3. Push the Space repository. Hugging Face builds the image and exposes the app URL when the build finishes.
4. Open `<space-url>/health`; it should return `{"ok": true}`. Then open the root URL and use the guided tour.

The first build downloads and bakes in the embedding model. Give the build extra time and use a Space hardware tier with enough disk and RAM for the Qdrant Edge shards and model.

## Public-demo behavior

The hosted service is deliberately an interactive demonstration. Device state, identities, and the hub log are ephemeral because the Docker image stores them under `/tmp/engram-data`. Visitors can reset the demo and run the attack simulations. Never use this Space for real device data, credentials, or private memories.

## Post-deploy smoke test

1. Confirm `/health` returns HTTP 200.
2. Visit the root page and wait for both device cards to show as ready.
3. Run the guided tour through the sync and attack steps.
4. Confirm a trusted search returns verified results and an attack lands in quarantine.
