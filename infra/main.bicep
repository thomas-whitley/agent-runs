// Container Apps on consumption, two apps, one Log Analytics workspace.
// No database resource: Postgres is Supabase, and its connection string is a
// secret on both apps. Everything scales to zero, which is what makes it free.

@description('Where to put everything. Australia East sits near the Supabase project.')
param location string = resourceGroup().location

@description('Prefix for every resource name.')
param name string = 'agent-runs'

@description('Container image for both roles, including the tag.')
param image string

@description('Supabase session pooler connection string.')
@secure()
param databaseUrl string

@description('Model API key. Empty runs the offline stub model.')
@secure()
param modelApiKey string = ''

@description('OpenAI compatible base URL for the model. Empty uses the stub.')
param modelBaseUrl string = ''

@description('Model id, or stub for the offline model.')
param model string = 'stub'

@description('Embedding key. Empty falls back to full text search.')
@secure()
param voyageApiKey string = ''

var logAnalyticsName = '${name}-logs'
var environmentName = '${name}-env'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      // The free grant is 5 GB a month. This will use megabytes, and the cap
      // stops a runaway from turning into a bill.
      dailyQuotaGb: json('0.1')
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${name}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Container Apps rejects a secret whose value is empty, so an unset key has to
// be left out of the array rather than passed through as ''.
var optionalSecrets = concat(
  empty(modelApiKey)
    ? []
    : [
        {
          name: 'model-api-key'
          value: modelApiKey
        }
      ],
  empty(voyageApiKey)
    ? []
    : [
        {
          name: 'voyage-api-key'
          value: voyageApiKey
        }
      ]
)

var sharedSecrets = concat(
  [
    {
      name: 'database-url'
      value: databaseUrl
    }
    {
      name: 'insights-connection-string'
      value: insights.properties.ConnectionString
    }
  ],
  optionalSecrets
)

// An env var referring to a secret that was left out fails the same way.
var modelEnvironment = concat(
  [
    {
      name: 'MODEL'
      value: model
    }
    {
      name: 'MODEL_BASE_URL'
      value: modelBaseUrl
    }
  ],
  empty(modelApiKey)
    ? []
    : [
        {
          name: 'MODEL_API_KEY'
          secretRef: 'model-api-key'
        }
      ],
  empty(voyageApiKey)
    ? []
    : [
        {
          name: 'VOYAGE_API_KEY'
          secretRef: 'voyage-api-key'
        }
      ]
)

var sharedEnvironment = [
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    secretRef: 'insights-connection-string'
  }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-api'
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        // An SSE stream is long lived, so the idle timeout matters more than
        // it would for a request and response service.
        allowInsecure: false
      }
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(sharedEnvironment, [
            {
              name: 'ROLE'
              value: 'api'
            }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                // Low on purpose: two open streams brings up the second replica.
                concurrentRequests: '2'
              }
            }
          }
        ]
      }
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-worker'
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: image
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(sharedEnvironment, [
            {
              name: 'ROLE'
              value: 'worker'
            }
          ], modelEnvironment)
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'pending-runs'
            custom: {
              type: 'postgresql'
              metadata: {
                query: 'SELECT count(*) FROM runs WHERE finished_at IS NULL'
                targetQueryValue: '1'
              }
              auth: [
                {
                  secretRef: 'database-url'
                  triggerParameter: 'connection'
                }
              ]
            }
          }
        ]
      }
    }
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output apiName string = api.name
output workerName string = worker.name
