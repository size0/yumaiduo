FROM node:22-bookworm-slim

ENV NODE_ENV=production
WORKDIR /app

COPY --chown=node:node package.json package-lock.json ./
COPY --chown=node:node vendor/plugin-sdk-server ./vendor/plugin-sdk-server
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

COPY --chown=node:node yumaiduo.plugin.json index.mjs ./
COPY --chown=node:node src ./src
COPY --chown=node:node ui ./ui
COPY --chown=node:node datasets/labels ./datasets/labels
COPY --chown=node:node datasets/splits ./datasets/splits
COPY --chown=node:node datasets/reports ./datasets/reports
RUN mkdir -p /app/data && chown node:node /app/data

USER node
VOLUME ["/app/data"]
EXPOSE 4003
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||4003)+'/healthz').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "index.mjs"]
