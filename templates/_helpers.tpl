{{/*
Expand the name of the chart.
*/}}
{{- define "nebari-pi-pack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "nebari-pi-pack.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "nebari-pi-pack.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "nebari-pi-pack.labels" -}}
helm.sh/chart: {{ include "nebari-pi-pack.chart" . }}
{{ include "nebari-pi-pack.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "nebari-pi-pack.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nebari-pi-pack.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of secret storing Pi integration tokens (m4 tools token).
*/}}
{{- define "nebari-pi-pack.pi-secret-name" -}}
{{- default (printf "%s-pi-secrets" (include "nebari-pi-pack.fullname" .)) .Values.pi.m4Tools.tokenSecretName -}}
{{- end }}

{{/*
Relay config/secret resource names.
*/}}
{{- define "nebari-pi-pack.relay-config-name" -}}
{{- default (printf "%s-relay-config" (include "nebari-pi-pack.fullname" .)) .Values.relay.configMapName -}}
{{- end }}

{{- define "nebari-pi-pack.relay-secret-name" -}}
{{- default (printf "%s-relay-secrets" (include "nebari-pi-pack.fullname" .)) .Values.relay.secretName -}}
{{- end }}

{{- define "nebari-pi-pack.relay-host" -}}
{{- default .Values.nebariapp.hostname .Values.relay.host -}}
{{- end }}
