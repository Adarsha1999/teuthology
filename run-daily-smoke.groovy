#!/usr/bin/env groovy
/**
 * Script to run smoke suite daily for both main and tentacle branches
 * Usage: groovy run-daily-smoke.groovy [override_yaml] [--jenkins] [--jenkins-url URL] [--jenkins-job JOB] [--jenkins-user USER] [--jenkins-token TOKEN]
 * Options:
 *   --jenkins              Trigger Jenkins pipeline instead of running directly
 *   --jenkins-url URL      Jenkins server URL (default: from JENKINS_URL env var or http://localhost:8080)
 *   --jenkins-job JOB      Jenkins job name (default: from JENKINS_JOB env var or 'daily-smoke')
 *   --jenkins-user USER    Jenkins username (default: from JENKINS_USER env var)
 *   --jenkins-token TOKEN  Jenkins API token (default: from JENKINS_TOKEN env var)
 * This script can be called manually or via cron
 */

@Grab('org.apache.httpcomponents:httpclient:4.5.14')
import org.apache.http.client.methods.*
import org.apache.http.entity.*
import org.apache.http.impl.client.*
import org.apache.http.auth.*
import org.apache.http.*
import org.apache.http.util.EntityUtils
import java.net.URI
import java.text.SimpleDateFormat
import groovy.json.JsonSlurper

// Parse command line arguments
def overrideYaml = "/home/ubuntu/override.yaml"
def triggerJenkins = false
def jenkinsUrl = System.getenv("JENKINS_URL") ?: "http://localhost:8080"
def jenkinsJob = System.getenv("JENKINS_JOB") ?: "daily-smoke"
def jenkinsUser = System.getenv("JENKINS_USER") ?: ""
def jenkinsToken = System.getenv("JENKINS_TOKEN") ?: ""

def i = 0
while (i < args.length) {
    if (args[i] == "--jenkins") {
        triggerJenkins = true
    } else if (args[i] == "--jenkins-url" && i + 1 < args.length) {
        jenkinsUrl = args[++i]
    } else if (args[i] == "--jenkins-job" && i + 1 < args.length) {
        jenkinsJob = args[++i]
    } else if (args[i] == "--jenkins-user" && i + 1 < args.length) {
        jenkinsUser = args[++i]
    } else if (args[i] == "--jenkins-token" && i + 1 < args.length) {
        jenkinsToken = args[++i]
    } else if (!args[i].startsWith("--")) {
        overrideYaml = args[i]
    }
    i++
}

// Configuration
def scriptDir = "/home/ubuntu/teuthology"
def logDir = "${scriptDir}/logs"
def timestamp = new SimpleDateFormat("yyyyMMdd-HHmmss").format(new Date())
def logFile = new File("${logDir}/daily-smoke-${timestamp}.log")

// Create logs directory if it doesn't exist
new File(logDir).mkdirs()

// Change to script directory
System.setProperty("user.dir", scriptDir)

// Function to log with timestamp
def log(message) {
    def timestamp = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date())
    def logMessage = "[${timestamp}] ${message}"
    println logMessage
    logFile.append("${logMessage}\n")
}

// Function to trigger Jenkins pipeline
def triggerJenkinsPipeline() {
    log("Triggering Jenkins pipeline: ${jenkinsJob}")
    
    if (!jenkinsUser || !jenkinsToken) {
        log("ERROR: Jenkins credentials not provided. Set JENKINS_USER and JENKINS_TOKEN environment variables or use --jenkins-user and --jenkins-token flags")
        return false
    }
    
    try {
        // Build Jenkins API URL
        def jobUrl = "${jenkinsUrl}/job/${jenkinsJob}/buildWithParameters"
        
        // Create HTTP client with authentication
        def client = HttpClients.createDefault()
        def request = new HttpPost(jobUrl)
        
        // Add basic authentication
        def credentials = new UsernamePasswordCredentials(jenkinsUser, jenkinsToken)
        def authScope = new AuthScope(AuthScope.ANY_HOST, AuthScope.ANY_PORT, AuthScope.ANY_REALM)
        def credsProvider = new BasicCredentialsProvider()
        credsProvider.setCredentials(authScope, credentials)
        
        def authCache = new BasicAuthCache()
        def basicAuth = new BasicScheme()
        def targetHost = new HttpHost(new URI(jenkinsUrl).getHost(), new URI(jenkinsUrl).getPort(), new URI(jenkinsUrl).getScheme())
        authCache.put(targetHost, basicAuth)
        
        def context = new HttpClientContext()
        context.setCredentialsProvider(credsProvider)
        context.setAuthCache(authCache)
        
        // Add parameters
        def params = new ArrayList<NameValuePair>()
        params.add(new BasicNameValuePair("OVERRIDE_YAML", overrideYaml))
        request.setEntity(new UrlEncodedFormEntity(params))
        
        // Execute request
        log("Sending request to: ${jobUrl}")
        def response = client.execute(request, context)
        def statusCode = response.getStatusLine().getStatusCode()
        
        if (statusCode == 201 || statusCode == 200) {
            // Get queue item URL from Location header
            def locationHeader = response.getFirstHeader("Location")
            if (locationHeader) {
                def queueUrl = locationHeader.getValue()
                log("✓ Jenkins pipeline triggered successfully")
                log("Queue URL: ${queueUrl}")
                
                // Extract queue item number
                def queueMatcher = queueUrl =~ /\/queue\/item\/(\d+)/
                if (queueMatcher.find()) {
                    def queueId = queueMatcher.group(1)
                    log("Queue item ID: ${queueId}")
                    
                    // Wait for job to be scheduled and get build number
                    log("Waiting for job to be scheduled...")
                    Thread.sleep(5000)
                    
                    def buildNumber = getBuildNumberFromQueue(queueId)
                    if (buildNumber) {
                        log("Build number: ${buildNumber}")
                        log("Build URL: ${jenkinsUrl}/job/${jenkinsJob}/${buildNumber}/")
                        return true
                    } else {
                        log("WARNING: Could not determine build number, but job was triggered")
                        return true
                    }
                }
            }
            return true
        } else {
            def responseBody = ""
            try {
                def entity = response.getEntity()
                if (entity) {
                    responseBody = EntityUtils.toString(entity, "UTF-8")
                }
            } catch (Exception e) {
                // Ignore
            }
            log("ERROR: Failed to trigger Jenkins pipeline. Status: ${statusCode}")
            if (responseBody) {
                log("Response: ${responseBody}")
            }
            return false
        }
    } catch (Exception e) {
        log("ERROR: Exception triggering Jenkins pipeline: ${e.message}")
        e.printStackTrace()
        return false
    }
}

// Function to get build number from queue item
def getBuildNumberFromQueue(queueId) {
    try {
        def queueUrl = "${jenkinsUrl}/queue/item/${queueId}/api/json"
        def client = HttpClients.createDefault()
        def request = new HttpGet(queueUrl)
        
        // Add authentication
        def credentials = new UsernamePasswordCredentials(jenkinsUser, jenkinsToken)
        def authScope = new AuthScope(AuthScope.ANY_HOST, AuthScope.ANY_PORT, AuthScope.ANY_REALM)
        def credsProvider = new BasicCredentialsProvider()
        credsProvider.setCredentials(authScope, credentials)
        
        def authCache = new BasicAuthCache()
        def basicAuth = new BasicScheme()
        def targetHost = new HttpHost(new URI(jenkinsUrl).getHost(), new URI(jenkinsUrl).getPort(), new URI(jenkinsUrl).getScheme())
        authCache.put(targetHost, basicAuth)
        
        def context = new HttpClientContext()
        context.setCredentialsProvider(credsProvider)
        context.setAuthCache(authCache)
        
        def response = client.execute(request, context)
        if (response.getStatusLine().getStatusCode() == 200) {
            def entity = response.getEntity()
            if (entity) {
                def jsonText = EntityUtils.toString(entity, "UTF-8")
                def json = new JsonSlurper().parseText(jsonText)
                if (json.executable) {
                    return json.executable.number.toString()
                }
            }
        }
    } catch (Exception e) {
        log("WARNING: Could not get build number from queue: ${e.message}")
    }
    return null
}

// Function to construct run name
def constructRunName(suite, timestamp, cephBranch) {
    def user = System.getProperty("user.name")
    def kernelBranch = "distro"
    def worker = "openstack"
    def flavor = "default"
    
    suite = suite.replaceAll("/", ":")
    return "${user}-${timestamp}-${suite}-${cephBranch}-${kernelBranch}-${flavor}-${worker}"
}

// Function to run smoke suite for a branch
def runSmokeForBranch(branch) {
    log("Starting smoke suite for branch: ${branch}")
    
    // Get shaman_id for the branch
    def shamanId
    try {
        def proc = ["python3", "getUpstreamBuildDetails.py",
                    "--branch", branch,
                    "--platform", "ubuntu-jammy-default,centos-9-default",
                    "--arch", "x86_64"].execute(null, new File(scriptDir))
        proc.waitFor()
        
        if (proc.exitValue() != 0) {
            def error = proc.err.text
            log("ERROR: Failed to get upstream build details for branch ${branch}:")
            log(error)
            return false
        }
        
        shamanId = proc.text.trim()
        log("Using shaman build id for branch ${branch}: ${shamanId}")
    } catch (Exception e) {
        log("ERROR: Exception getting shaman_id for branch ${branch}: ${e.message}")
        return false
    }
    
    // Upload shaman_id to remote server
    try {
        def sshCmd = "sudo mkdir -p /data/scheduler/cron && echo '${shamanId}' | sudo tee /data/scheduler/cron/${branch}-${new SimpleDateFormat('yyyy-MM-dd').format(new Date())} > /dev/null"
        def proc = ["sshpass", "-p", "admin", "ssh", "-o", "StrictHostKeyChecking=no",
                    "cloud-user@10.0.196.233", sshCmd].execute()
        proc.waitFor()
        def output = proc.text
        if (output) {
            log(output)
        }
    } catch (Exception e) {
        log("WARNING: Failed to upload shaman_id: ${e.message}")
    }
    
    // Unlock targets before running
    log("Unlocking targets...")
    try {
        def listProc = ["teuthology-lock", "--list-targets", "--owner", "scheduled_ubuntu@teuth-teuthology"].execute(null, new File(scriptDir))
        listProc.waitFor()
        if (listProc.exitValue() == 0) {
            new File("${System.getProperty('user.home')}/locked_targets").text = listProc.text
        } else {
            log("WARNING: Failed to list targets, continuing anyway...")
            log(listProc.err.text)
        }
    } catch (Exception e) {
        log("WARNING: Failed to list targets, continuing anyway...")
    }
    
    try {
        def unlockProc = ["teuthology-lock", "--owner", "scheduled_ubuntu@teuth-teuthology",
                          "--unlock", "-t", "${System.getProperty('user.home')}/locked_targets", "-vvv"].execute(null, new File(scriptDir))
        unlockProc.waitFor()
        def output = unlockProc.text + unlockProc.err.text
        if (output) {
            logFile.append(output)
        }
        if (unlockProc.exitValue() != 0) {
            log("WARNING: Failed to unlock targets, continuing anyway...")
        }
    } catch (Exception e) {
        log("WARNING: Failed to unlock targets, continuing anyway...")
    }
    
    // Smoke suite configuration
    def suite = "smoke"
    def seed = 8446
    
    log("Starting smoke suite for branch ${branch} with seed=${seed}")
    
    // Capture timestamp
    def timestamp = new SimpleDateFormat("yyyy-MM-dd_HH:mm:ss").format(new Date())
    
    // Build and run command
    def cmd = ["teuthology-suite",
               "--suite", suite,
               "--machine-type", "openstack",
               "--ceph", branch,
               "--ceph-repo", "https://github.com/ceph/ceph",
               "--priority", "50",
               "--force-priority",
               "--seed", seed.toString(),
               "--sha1", shamanId,
               overrideYaml]
    
    log("Running command: ${cmd.join(' ')}")
    
    // Execute teuthology-suite and capture output
    def suiteOutput
    try {
        def proc = cmd.execute(null, new File(scriptDir))
        proc.waitFor()
        suiteOutput = proc.text + proc.err.text
        logFile.append(suiteOutput)
        
        if (proc.exitValue() != 0) {
            log("ERROR: Failed to schedule smoke suite for branch ${branch}")
            return false
        }
    } catch (Exception e) {
        log("ERROR: Exception running teuthology-suite: ${e.message}")
        return false
    }
    
    // Extract run name from teuthology-suite output
    // Format: "Job scheduled with name <run_name> and ID <id>"
    def runName
    def matcher = suiteOutput =~ /Job scheduled with name (\S+)/
    if (matcher.find()) {
        runName = matcher.group(1)
    } else {
        log("WARNING: Could not extract run name from teuthology-suite output, constructing it...")
        runName = constructRunName(suite, timestamp, branch)
    }
    
    log("Using run name: ${runName}")
    
    // Wait for run to be registered
    log("Waiting 10 seconds for run to be registered on server...")
    Thread.sleep(10000)
    
    // Verify run exists
    log("Verifying run exists on server...")
    def runExists = false
    def maxAttempts = 6
    def attempt = 0
    
    while (attempt < maxAttempts) {
        try {
            def verifyScript = """
from teuthology.report import ResultsReporter
try:
    reporter = ResultsReporter()
    jobs = reporter.get_jobs('${runName}', fields=['job_id'])
    if jobs is not None:
        exit(0)
except Exception as e:
    if '404' in str(e) or 'Not Found' in str(e):
        exit(1)
    exit(0)
"""
            def proc = ["python3", "-c", verifyScript].execute(null, new File(scriptDir))
            proc.waitFor()
            
            if (proc.exitValue() == 0) {
                runExists = true
                break
            }
        } catch (Exception e) {
            // Continue trying
        }
        
        attempt++
        if (attempt < maxAttempts) {
            log("Run not found yet, waiting 5 seconds... (attempt ${attempt}/${maxAttempts})")
            Thread.sleep(5000)
        }
    }
    
    if (!runExists) {
        log("WARNING: Could not verify run '${runName}' exists after ${maxAttempts} attempts.")
        log("Will attempt to wait anyway - teuthology-wait will handle this.")
    } else {
        log("Run verified on server: ${runName}")
    }
    
    // Wait for suite to complete using teuthology-wait
    log("Waiting for smoke suite (branch: ${branch}, run: ${runName}) to complete using teuthology-wait...")
    try {
        def proc = ["teuthology-wait", "--run", runName].execute(null, new File(scriptDir))
        proc.waitFor()
        def output = proc.text + proc.err.text
        logFile.append(output)
        
        if (proc.exitValue() != 0) {
            log("WARNING: Smoke suite for branch ${branch} completed with failures or errors")
            return false
        } else {
            log("✓ Smoke suite for branch ${branch} completed successfully (teuthology-wait confirmed completion)")
            return true
        }
    } catch (Exception e) {
        log("ERROR: Exception waiting for suite: ${e.message}")
        return false
    }
}

// Main execution
log("==========================================")
log("Starting daily smoke suite execution")
log("Log file: ${logFile.absolutePath}")
if (triggerJenkins) {
    log("Mode: Triggering Jenkins pipeline")
    log("Jenkins URL: ${jenkinsUrl}")
    log("Jenkins Job: ${jenkinsJob}")
} else {
    log("Mode: Direct execution")
}
log("==========================================")
log("")

// If Jenkins mode, trigger pipeline and exit
if (triggerJenkins) {
    if (triggerJenkinsPipeline()) {
        log("✓ Jenkins pipeline triggered successfully")
        log("==========================================")
        log("Daily smoke suite execution completed")
        log("==========================================")
        System.exit(0)
    } else {
        log("✗ Failed to trigger Jenkins pipeline")
        log("==========================================")
        log("Daily smoke suite execution completed with errors")
        log("==========================================")
        System.exit(1)
    }
}

// Get day of week (1=Monday, 7=Sunday)
def calendar = Calendar.getInstance()
def dayOfWeek = calendar.get(Calendar.DAY_OF_WEEK)
def dayName = new SimpleDateFormat("EEEE").format(new Date())

// Convert to ISO day of week (1=Monday, 7=Sunday)
def isoDayOfWeek = dayOfWeek == Calendar.SUNDAY ? 7 : dayOfWeek - 1

// Check if tentacle should run (Monday=1, Wednesday=3)
def runTentacle = false
if (isoDayOfWeek == 1 || isoDayOfWeek == 3) {
    runTentacle = true
    log("Today is ${dayName} - will run both tentacle and main branches")
} else {
    log("Today is ${dayName} - will run main branch only (tentacle runs only on Monday and Wednesday)")
}
log("")

// Run tentacle branch first if it's Monday or Wednesday
if (runTentacle) {
    log("Starting smoke suite for 'tentacle' branch...")
    log("")
    if (runSmokeForBranch("tentacle")) {
        log("✓ Smoke suite for 'tentacle' branch completed")
    } else {
        log("✗ Smoke suite for 'tentacle' branch had errors")
    }
    log("")
    log("Starting smoke suite for 'main' branch (tentacle run has completed)...")
    log("")
    if (runSmokeForBranch("main")) {
        log("✓ Smoke suite for 'main' branch completed")
    } else {
        log("✗ Smoke suite for 'main' branch had errors")
    }
} else {
    log("Starting smoke suite for 'main' branch...")
    log("")
    if (runSmokeForBranch("main")) {
        log("✓ Smoke suite for 'main' branch completed")
    } else {
        log("✗ Smoke suite for 'main' branch had errors")
    }
}
log("")

log("==========================================")
log("Daily smoke suite execution completed")
log("==========================================")
