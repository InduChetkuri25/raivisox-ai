# RAIVISOX Web — production multi-stage image
# NEXT_PUBLIC_API_URL is baked in at BUILD time (Next.js inlines it).

# ---- build stage -----------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
# public/ is optional; create it so COPY below always resolves.
RUN mkdir -p public
RUN npm run build

# ---- runner stage (production only) ---------------------------------------
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

COPY --from=builder /app/.next ./.next
COPY --from=builder /app/next.config.js ./
COPY --from=builder /app/public ./public

USER node
EXPOSE 3000

CMD ["npm", "run", "start"]
