FROM node:20-alpine as build

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci
RUN node -e "require.resolve('supertokens-auth-react/package.json')"

# Copy source code
COPY . .

# Build-time configuration (baked into the static bundle).
# Optional: values here take precedence over frontend/.env. When left empty, frontend/.env
# (if present) is used, which is how the CI/CD pipeline supplies them.
ARG VITE_SUPERTOKENS_API_DOMAIN=""
ARG VITE_SUPERTOKENS_WEBSITE_DOMAIN=""
RUN set -e; \
    : > .env.production.local; \
    if [ -n "$VITE_SUPERTOKENS_API_DOMAIN" ]; then \
      echo "VITE_SUPERTOKENS_API_DOMAIN=$VITE_SUPERTOKENS_API_DOMAIN" >> .env.production.local; \
    fi; \
    if [ -n "$VITE_SUPERTOKENS_WEBSITE_DOMAIN" ]; then \
      echo "VITE_SUPERTOKENS_WEBSITE_DOMAIN=$VITE_SUPERTOKENS_WEBSITE_DOMAIN" >> .env.production.local; \
    fi

# Build the app
RUN npm run build

# Production stage
FROM nginx:alpine

# Copy built assets from build stage
COPY --from=build /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expose port 80
EXPOSE 80

# Health check — use 127.0.0.1 explicitly to avoid IPv6 resolution of "localhost"
HEALTHCHECK --interval=30s --timeout=3s \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1/ || exit 1

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
