// Container Apps on consumption, two apps, one scheduled Job, one Log
// Analytics workspace.
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

@description('Bearer token for every non-public endpoint. Empty fails closed.')
@secure()
param mercuryBearerToken string = ''

@description('PageSpeed Insights key for the cloud Lighthouse fallback. Empty sends keyless requests, whose shared quota is often spent.')
@secure()
param pagespeedApiKey string = ''

@description('Seconds a Lighthouse check waits for the self hosted worker before the cloud worker takes it.')
param checkClaimWindowSeconds int = 1800

@description('mercury.yaml, base64 encoded, for the scheduler. The private config repo, which runs this template, passes its real one.')
@secure()
param mercuryConfigB64 string = ''

// Without a token the scheduler could only start and fail every hour.
var deployScheduler = !empty(mercuryBearerToken)

// Per docs/mercury.md the real config lives only in the private repo, which is
// the only thing that deploys this template. A deploy by hand without the
// config gets a placeholder, and the Job then checks no sites.
var schedulerConfigB64 = empty(mercuryConfigB64)
  ? base64('portfolio:\n  sites: []\n')
  : mercuryConfigB64

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
      ],
  empty(mercuryBearerToken)
    ? []
    : [
        {
          name: 'mercury-bearer-token'
          value: mercuryBearerToken
        }
      ]
)

var coreSecrets = [
  {
    name: 'database-url'
    value: databaseUrl
  }
  {
    name: 'insights-connection-string'
    value: insights.properties.ConnectionString
  }
]

var sharedSecrets = concat(coreSecrets, optionalSecrets)

// The worker alone calls PageSpeed, so the key goes to it alone.
var workerSecrets = concat(
  sharedSecrets,
  empty(pagespeedApiKey)
    ? []
    : [
        {
          name: 'pagespeed-api-key'
          value: pagespeedApiKey
        }
      ]
)

var workerCheckEnvironment = concat(
  [
    {
      name: 'CHECK_CLAIM_WINDOW'
      value: string(checkClaimWindowSeconds)
    }
  ],
  empty(pagespeedApiKey)
    ? []
    : [
        {
          name: 'PAGESPEED_API_KEY'
          secretRef: 'pagespeed-api-key'
        }
      ]
)

// DEFAULT_LEASE_SECONDS in app/config.py. The worker gets no LEASE_SECONDS, so
// this has to match that default.
var leaseSeconds = 120

// What wakes the worker: any unfinished run it executes, a Lighthouse check
// that waited out the claim window (from creation, or from a lapsed lease),
// and a check the cloud path is running, so the worker is not scaled away
// mid call. A check still inside its window does not count, or the worker
// would sit awake for the whole window every week. This mirrors _CLAIMABLE in
// app/worker.py.
var pendingWorkQuery = 'SELECT count(*) FROM runs WHERE finished_at IS NULL AND (type <> \'site_check\' OR (type = \'site_check\' AND check_kind = \'lighthouse\' AND (executor = \'cloud\' OR (claimed_by IS NULL AND created_at < now() - make_interval(secs => ${checkClaimWindowSeconds})) OR (status = \'running\' AND heartbeat_at < now() - make_interval(secs => ${leaseSeconds + checkClaimWindowSeconds})))))'

// The scheduler makes no model call, so it gets no model or embedding key,
// and the config goes to it alone. Only used when deployScheduler is true, so
// the token cannot be empty here.
var schedulerSecrets = concat(coreSecrets, [
  {
    name: 'mercury-bearer-token'
    value: mercuryBearerToken
  }
  {
    name: 'mercury-config'
    value: schedulerConfigB64
  }
])

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
          env: concat(sharedEnvironment, empty(mercuryBearerToken) ? [] : [
            {
              name: 'MERCURY_BEARER_TOKEN'
              secretRef: 'mercury-bearer-token'
            }
          ], [
            {
              name: 'ROLE'
              value: 'api'
            }
            {
              // Resource.create() reads this when configure_azure_monitor is not
              // given an explicit resource. Without it both roles land under the
              // SDK default name and traces cannot be told apart by cloud_RoleName.
              name: 'OTEL_SERVICE_NAME'
              value: '${name}-api'
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
      secrets: workerSecrets
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
            {
              name: 'OTEL_SERVICE_NAME'
              value: '${name}-worker'
            }
          ], modelEnvironment, workerCheckEnvironment)
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
                query: pendingWorkQuery
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

resource scheduler 'Microsoft.App/jobs@2024-03-01' = if (deployScheduler) {
  name: '${name}-scheduler'
  location: location
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      // Hourly, matching config/mercury.sample.yaml's site_uptime. Reading a
      // cron per entry from mercury.yaml waits until a second schedule needs
      // a different cadence.
      scheduleTriggerConfig: {
        cronExpression: '0 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 300
      replicaRetryLimit: 0
      secrets: schedulerSecrets
    }
    template: {
      containers: [
        {
          name: 'scheduler'
          image: image
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(sharedEnvironment, [
            {
              name: 'ROLE'
              value: 'scheduler'
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: '${name}-scheduler'
            }
            {
              name: 'API_BASE_URL'
              value: 'https://${api.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'MERCURY_BEARER_TOKEN'
              secretRef: 'mercury-bearer-token'
            }
            {
              name: 'MERCURY_CONFIG_B64'
              secretRef: 'mercury-config'
            }
          ])
        }
      ]
    }
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output apiName string = api.name
output workerName string = worker.name
output schedulerName string = deployScheduler ? scheduler.name : ''
