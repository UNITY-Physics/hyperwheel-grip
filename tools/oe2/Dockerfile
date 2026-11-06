# Orthanc Explorer 2 for Raspberry Pi OS Trixie (arm64), built from a pinned commit.
# Static build: the plugin needs nothing on the Pi beyond the C/C++ runtime. The web
# application is built from source with npm ci (OE2's own lock file), not downloaded.
FROM debian:trixie@sha256:9c6e381421d0f48d1b8133e573f33c185ee16cbf505eda9c4a8a4791b25ea8a6

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      build-essential cmake git ca-certificates python3 unzip patch curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

ARG OE2_VERSION
ARG OE2_COMMIT
RUN git clone -q --depth 1 --branch "${OE2_VERSION}" https://github.com/orthanc-server/orthanc-explorer-2.git /src \
    && test "$(git -C /src rev-parse HEAD)" = "${OE2_COMMIT}" \
    && git -C /src archive --format=tar.gz --prefix="orthanc-explorer-2-${OE2_VERSION}/" -o /oe2-source.tar.gz HEAD

WORKDIR /src/WebApplication
RUN npm ci --no-audit --no-fund && npm run build

WORKDIR /build
RUN cmake /src -DCMAKE_BUILD_TYPE=Release -DPLUGIN_VERSION="${OE2_VERSION}" -DSTATIC_BUILD=ON \
      -DUSE_SYSTEM_ORTHANC_SDK=OFF -DALLOW_DOWNLOADS=ON -DWEBAPP_DIST_SOURCE=LOCAL \
    && make -j"$(nproc)" \
    && strip libOrthancExplorer2.so*

RUN mkdir /out && cp -L /build/libOrthancExplorer2.so /out/ && cp /oe2-source.tar.gz /src/LICENSE /out/ \
    && printf '%s\n' "${OE2_VERSION}" > /out/VERSION \
    && printf 'Orthanc Explorer 2 %s, built from https://github.com/orthanc-server/orthanc-explorer-2 commit %s.\nLicence: AGPL-3.0 (see LICENSE). The exact source it was built from is in oe2-source.tar.gz.\n' \
         "${OE2_VERSION}" "${OE2_COMMIT}" > /out/SOURCE
