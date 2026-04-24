{{- define "chores.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "chores.labels" -}}
app.kubernetes.io/name: {{ include "chores.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "chores.selectorLabels" -}}
app.kubernetes.io/name: {{ include "chores.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
