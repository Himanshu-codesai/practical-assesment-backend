FROM node:22-slim

# The gateway runs the Claude command-line tool once per request.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /srv
COPY server.mjs .

ENV HOST=0.0.0.0 \
    PORT=8787
USER node
EXPOSE 8787
CMD ["node", "server.mjs"]
