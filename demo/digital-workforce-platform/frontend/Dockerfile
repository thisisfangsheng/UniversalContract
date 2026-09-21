FROM node:20-alpine AS build
WORKDIR /app
COPY demo/digital-workforce-platform/frontend/package*.json ./
RUN npm ci
COPY demo/digital-workforce-platform/frontend ./
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY demo/digital-workforce-platform/frontend/nginx.conf /etc/nginx/conf.d/default.conf
