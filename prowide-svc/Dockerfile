# =============================================================================
# Prowide REST 마이크로서비스 — Java 21 / Spring Boot 3.3
# Multi-stage build: maven-builder → jre-runtime
#
# 스테이지 구성:
#   builder  : 공식 Maven 이미지 — pom.xml 먼저 복사해 의존성 레이어 캐싱
#   runtime  : 최소 JRE 이미지 — root 미사용, appuser 실행
# =============================================================================

# ── Stage 1: Maven 빌드 ────────────────────────────────────────────────────────
# 공식 Maven 이미지 사용 → apt-get 으로 Maven 설치하는 방식 대비
#   · 레이어 크기 감소 (mvn 바이너리만 포함)
#   · Maven 버전 명시로 재현성(reproducibility) 보장
FROM maven:3.9.6-eclipse-temurin-21-jammy AS builder

WORKDIR /build

# ── Layer cache 최적화 핵심 ────────────────────────────────────────────────────
# 1) pom.xml 만 먼저 복사
COPY pom.xml .

# 2) 의존성 전체 로컬 캐시(.m2) 에 다운로드
#    pom.xml 이 변경되지 않으면 이 레이어(~수백 MB)가 재사용됨
#    소스 변경만으로는 이 레이어가 무효화되지 않아 빌드 시간 대폭 단축
RUN mvn dependency:go-offline \
        --batch-mode \
        --quiet \
        -Dmaven.wagon.http.retryHandler.count=3

# 3) 소스 복사 후 패키지 빌드
#    소스가 바뀌어도 의존성 레이어(Step 2)는 재사용
COPY src ./src
RUN mvn package \
        --batch-mode \
        --quiet \
        -DskipTests \
        -Dmaven.test.skip=true


# ── Stage 2: 최소 JRE 런타임 이미지 ───────────────────────────────────────────
# eclipse-temurin:21-jre-jammy: JDK 없이 JRE 만 포함 → 이미지 약 60% 감소
FROM eclipse-temurin:21-jre-jammy AS runtime

# 비-root 사용자 생성 (UID/GID 1001 고정)
RUN groupadd -r appgroup --gid 1001 \
    && useradd -r -g appgroup --uid 1001 -d /app -s /sbin/nologin appuser

# curl — HEALTHCHECK 용도
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 빌더에서 실행 가능한 fat JAR 복사
COPY --from=builder /build/target/prowide-svc-*.jar app.jar
RUN chown appuser:appgroup app.jar

# 비-root 사용자로 전환
USER appuser

EXPOSE 8080

# Spring Boot Actuator 헬스체크
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -f http://localhost:8080/actuator/health || exit 1

# JVM 컨테이너 최적화 옵션:
#   -XX:+UseContainerSupport    : cgroup 메모리/CPU 제한 자동 인식 (Java 11+)
#   -XX:MaxRAMPercentage=75.0   : 컨테이너 메모리의 75% 를 JVM Heap 에 할당
#   -XX:+UseG1GC                : 저지연 GC (Spring Boot 3 권장)
#   -Djava.security.egd=...     : SecureRandom 블로킹 방지 (컨테이너 환경)
ENTRYPOINT ["java", \
    "-XX:+UseContainerSupport", \
    "-XX:MaxRAMPercentage=75.0", \
    "-XX:+UseG1GC", \
    "-XX:+ExitOnOutOfMemoryError", \
    "-Djava.security.egd=file:/dev/./urandom", \
    "-Dspring.profiles.active=${SPRING_PROFILES_ACTIVE:-docker}", \
    "-jar", "app.jar"]
