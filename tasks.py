import ipaddress
import os
import semver
import re
import shutil
import sys
import yaml
import tempfile
try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen

from invoke import run, task
from invoke.exceptions import Exit, UnexpectedExit

all_binaries = set(["controller",
                    "speaker",
                    "mirror-server"])
all_architectures = set(["amd64",
                         "arm",
                         "arm64",
                         "ppc64le",
                         "s390x"])

def _check_architectures(architectures):
    out = set()
    for arch in architectures:
        if arch == "all":
            out |= all_architectures
        elif arch not in all_architectures:
            print("unknown architecture {}".format(arch))
            print("Supported architectures: {}".format(", ".join(sorted(all_architectures))))
            sys.exit(1)
        else:
            out.add(arch)
    if not out:
        out.add("amd64")
    return list(sorted(out))

def _check_binaries(binaries):
    out = set()
    for binary in binaries:
        if binary == "all":
            out |= all_binaries
        elif binary not in all_binaries:
            print("Unknown binary {}".format(binary))
            print("Known binaries: {}".format(", ".join(sorted(all_binaries))))
            sys.exit(1)
        else:
            out.add(binary)
    if not out:
        out.add("controller")
        out.add("speaker")
    return list(sorted(out))

def _make_build_dirs():
    for arch in all_architectures:
        for binary in all_binaries:
            dir = os.path.join("build", arch, binary)
            if not os.path.exists(dir):
                os.makedirs(dir, mode=0o750)


@task(iterable=["binaries", "architectures"],
      help={
          "binaries": "binaries to build. One or more of {}, or 'all'".format(", ".join(sorted(all_binaries))),
          "architectures": "architectures to build. One or more of {}, or 'all'".format(", ".join(sorted(all_architectures))),
          "registry": "Docker registry under which to tag the images. Default 'quay.io'.",
          "repo": "Docker repository under which to tag the images. Default 'metallb'.",
          "tag": "Docker image tag prefix to use. Actual tag will be <tag>-<arch>. Default 'dev'.",
      })
def build(ctx, binaries, architectures, registry="quay.io", repo="metallb", tag="dev"):
    """Build MetalLB docker images."""
    binaries = _check_binaries(binaries)
    architectures = _check_architectures(architectures)
    _make_build_dirs()

    commit = run("git describe --dirty --always", hide=True).stdout.strip()
    branch = run("git rev-parse --abbrev-ref HEAD", hide=True).stdout.strip()

    for arch in architectures:
        env = {
            "CGO_ENABLED": "0",
            "GOOS": "linux",
            "GOARCH": arch,
            "GOARM": "6",
            "GO111MODULE": "on",
        }
        for bin in binaries:
            run("go build -v -o build/{arch}/{bin}/{bin} -ldflags "
                "'-X go.universe.tf/metallb/internal/version.gitCommit={commit} "
                "-X go.universe.tf/metallb/internal/version.gitBranch={branch}' "
                "go.universe.tf/metallb/{bin}".format(
                    arch=arch,
                    bin=bin,
                    commit=commit,
                    branch=branch),
                env=env,
                echo=True)
            run("docker build "
                "-t {registry}/{repo}/{bin}:{tag}-{arch} "
                "-f {bin}/Dockerfile build/{arch}/{bin}".format(
                    registry=registry,
                    repo=repo,
                    bin=bin,
                    tag=tag,
                    arch=arch),
                echo=True)


@task(iterable=["binaries", "architectures"],
      help={
          "binaries": "binaries to build. One or more of {}, or 'all'".format(", ".join(sorted(all_binaries))),
          "architectures": "architectures to build. One or more of {}, or 'all'".format(", ".join(sorted(all_architectures))),
          "registry": "Docker registry under which to tag the images. Default 'quay.io'.",
          "repo": "Docker repository under which to tag the images. Default 'metallb'.",
          "tag": "Docker image tag prefix to use. Actual tag will be <tag>-<arch>. Default 'dev'.",
      })
def push(ctx, binaries, architectures, registry="quay.io", repo="metallb", tag="dev"):
    """Build and push docker images to registry."""
    binaries = _check_binaries(binaries)
    architectures = _check_architectures(architectures)

    for arch in architectures:
        for bin in binaries:
            build(ctx, binaries=[bin], architectures=[arch], registry=registry, repo=repo, tag=tag)
            run("docker push {registry}/{repo}/{bin}:{tag}-{arch}".format(
                registry=registry,
                repo=repo,
                bin=bin,
                arch=arch,
                tag=tag),
                echo=True)


@task(iterable=["binaries"],
      help={
          "binaries": "binaries to build. One or more of {}, or 'all'".format(", ".join(sorted(all_binaries))),
          "registry": "Docker registry under which to tag the images. Default 'quay.io'.",
          "repo": "Docker repository under which to tag the images. Default 'metallb'.",
          "tag": "Docker image tag prefix to use. Actual tag will be <tag>-<arch>. Default 'dev'.",
      })
def push_multiarch(ctx, binaries, registry="quay.io", repo="metallb", tag="dev"):
    """Build and push multi-architecture docker images to registry."""
    binaries = _check_binaries(binaries)
    architectures = _check_architectures(["all"])
    push(ctx, binaries=binaries, architectures=architectures, registry=registry, repo=repo, tag=tag)

    platforms = ",".join("linux/{}".format(arch) for arch in architectures)
    for bin in binaries:
        run("manifest-tool push from-args "
            "--platforms {platforms} "
            "--template {registry}/{repo}/{bin}:{tag}-ARCH "
            "--target {registry}/{repo}/{bin}:{tag}".format(
                platforms=platforms,
                registry=registry,
                repo=repo,
                bin=bin,
                tag=tag),
            echo=True)


def validate_kind_version():
    """Validate minimum required version of kind."""
    # If kind is not installed, this first command will raise an UnexpectedExit
    # exception, and inv will exit at this point making it clear running "kind"
    # failed.
    min_version = "0.9.0"

    try:
        raw = run("kind version", echo=True)
    except Exception as e:
        raise Exit(message="Could not determine kind version (is kind installed?)")

    actual_version = re.search("v(\d*\.\d*\.\d*)", raw.stdout).group(1)
    delta = semver.compare(actual_version, min_version)

    if delta < 0:
        raise Exit(message="kind version >= {} required".format(min_version))


@task(help={
    "architecture": "CPU architecture of the local machine. Default 'amd64'.",
    "name": "name of the kind cluster to use.",
    "protocol": "Pre-configure MetalLB with the specified protocol. "
                "Unconfigured by default. Supported: 'bgp','layer2'",
})
def dev_env(ctx, architecture="amd64", name="kind", cni=None, protocol=None):
    """Build and run MetalLB in a local Kind cluster.

    If the cluster specified by --name (default "kind") doesn't exist,
    it is created. Then, build MetalLB docker images from the
    checkout, push them into kind, and deploy manifests/metallb.yaml
    to run those images.
    """

    validate_kind_version()

    clusters = run("kind get clusters", hide=True).stdout.strip().splitlines()
    mk_cluster = name not in clusters
    if mk_cluster:
        config = {
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "kind": "Cluster",
            "nodes": [{"role": "control-plane"},
                      {"role": "worker"},
                      {"role": "worker"},
            ],
        }
        if protocol == "layer2":
            config["networking"] = {
                "podSubnet": "10.240.0.0/17"
            }
        if cni:
            config["networking"] = {
                "disableDefaultCNI": True,
            }
        config = yaml.dump(config).encode("utf-8")
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(config)
            tmp.flush()
            run("kind create cluster --name={} --config={}".format(name, tmp.name), pty=True, echo=True)

    if mk_cluster and cni:
        run("kubectl apply -f e2etest/manifests/{}.yaml".format(cni), echo=True)

    build(ctx, binaries=["controller", "speaker", "mirror-server"], architectures=[architecture])
    run("kind load docker-image --name={} quay.io/metallb/controller:dev-{}".format(name, architecture), echo=True)
    run("kind load docker-image --name={} quay.io/metallb/speaker:dev-{}".format(name, architecture), echo=True)
    run("kind load docker-image --name={} quay.io/metallb/mirror-server:dev-{}".format(name, architecture), echo=True)

    run("kubectl delete po -nmetallb-system --all", echo=True)

    manifests_dir = os.getcwd() + "/manifests"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy namespace manifest.
        shutil.copy(manifests_dir + "/namespace.yaml", tmpdir)

        with open(manifests_dir + "/metallb.yaml") as f:
            manifest = f.read()
        manifest = manifest.replace(":main", ":dev-{}".format(architecture))
        with open(tmpdir + "/metallb.yaml", "w") as f:
            f.write(manifest)
            f.flush()

        run("kubectl apply -f {}/namespace.yaml".format(tmpdir), echo=True)
        run("kubectl apply -f {}/metallb.yaml".format(tmpdir), echo=True)

    with open("e2etest/manifests/mirror-server.yaml") as f:
        manifest = f.read()
    manifest = manifest.replace(":main", ":dev-{}".format(architecture))
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(manifest.encode("utf-8"))
        tmp.flush()
        run("kubectl apply -f {}".format(tmp.name), echo=True)

    if protocol == "bgp":
        print("Configuring MetalLB with a BGP test environment")
        bgp_dev_env()
    elif protocol == "layer2":
        print("Configuring MetalLB with a layer 2 test environment")
        layer2_dev_env()
    else:
        print("Leaving MetalLB unconfigured")


# Configure MetalLB in the dev-env for layer2 testing.
# Identify the unused network address range from kind network and used it in configmap.
def layer2_dev_env():
    # Get unused v4/v6 range for services
    network_subnet_list = run('docker network inspect -f "{{ '
            'range .IPAM.Config}}{{.Subnet}} {{end}}" kind', echo=True).stdout.strip().split(' ')
    used_ipv4_list = run('docker network inspect -f '
            '"{{range .Containers}}{{.IPv4Address}} {{end}}" kind', echo=True).stdout.strip().split(' ')
    used_ipv6_list = run('docker network inspect -f '
            '"{{range .Containers}}{{.IPv6Address}} {{end}}" kind', echo=True).stdout.strip().split(' ')
    service_range = {}
    for i in network_subnet_list:
        network = ipaddress.ip_network(i)
        if network.version == 6:
            used_list = sorted(used_ipv6_list)
        else:
            used_list = sorted(used_ipv4_list)

        # try to get 10 IP addresses after the last assigned node address in the kind network subnet
        # if failed, just quit (recreate kind cluster might solve the situation)
        service_ip_range_start = ipaddress.ip_interface(used_list[-1]) + 1
        service_ip_range_end = ipaddress.ip_interface(used_list[-1]) + 11
        if service_ip_range_start not in network:
            raise Exit(message='network range %s is not in %s' % (service_ip_range_start, network))
        if service_ip_range_end not in network:
            raise Exit(message='network range %s is not in %s' % (service_ip_range_end, network))
        service_range[network.version] = '%s-%s' % (service_ip_range_start.ip, service_ip_range_end.ip)

    dev_env_dir = os.getcwd() + "/dev-env/layer2"
    with open("%s/config.yaml.tmpl" % dev_env_dir, 'r') as f:
        layer2_config = "# THIS FILE IS AUTOGENERATED\n" + f.read()
    layer2_config = layer2_config.replace("SERVICE_V4_RANGE", service_range[4])
    layer2_config = layer2_config.replace("SERVICE_V6_RANGE", service_range[6])
    with open("%s/config.yaml" % dev_env_dir, 'w') as f:
        f.write(layer2_config)
    # Apply the MetalLB ConfigMap
    run("kubectl apply -f %s/config.yaml" % dev_env_dir)

# Configure MetalLB in the dev-env for BGP testing. Start an frr based BGP
# router in a container and configure MetalLB to peer with it.
# See dev-env/bgp/README.md for some more information.
def bgp_dev_env():
    dev_env_dir = os.getcwd() + "/dev-env/bgp"
    frr_volume_dir = dev_env_dir + "/frr-volume"

    # TODO -- The IP address handling will need updates to add support for IPv6

    # We need the IPs for each Node in the cluster to place them in the BGP
    # router configuration file (bgpd.conf). Each Node will peer with this
    # router.
    node_ips = run("kubectl get nodes -o jsonpath='{.items[*].status.addresses"
            "[?(@.type==\"InternalIP\")].address}{\"\\n\"}'", echo=True)
    node_ips = node_ips.stdout.strip().split()
    if len(node_ips) != 3:
        raise Exit(message='Expected 3 nodes, got %d' % len(node_ips))

    # Create a new directory that will be used as the config volume for frr.
    try:
        # sudo because past docker runs will have changed ownership of this dir
        run('sudo rm -rf "%s"' % frr_volume_dir)
        os.mkdir(frr_volume_dir)
    except FileExistsError:
        pass
    except Exception as e:
        raise Exit(message='Failed to create frr-volume directory: %s'
                   % str(e))

    # These config files are static, so we copy them straight in.
    copy_files = ('zebra.conf', 'daemons', 'vtysh.conf')
    for f in copy_files:
        shutil.copyfile("%s/frr/%s" % (dev_env_dir, f),
                        "%s/%s" % (frr_volume_dir, f))

    # bgpd.conf is created from a template so that we can include the current
    # Node IPs.
    with open("%s/frr/bgpd.conf.tmpl" % dev_env_dir, 'r') as f:
        bgpd_config = "! THIS FILE IS AUTOGENERATED\n" + f.read()
    for n in range(0, len(node_ips)):
        bgpd_config = bgpd_config.replace("NODE%d_IP" % n, node_ips[n])
    with open("%s/bgpd.conf" % frr_volume_dir, 'w') as f:
        f.write(bgpd_config)

    # Run a BGP router in a container for all of the speakers to peer with.
    run('for frr in $(docker ps -a -f name=frr --format {{.Names}}) ; do '
        '    docker rm -f $frr ; '
        'done', echo=True)
    run("docker run -d --privileged --network kind --rm --name frr --volume %s:/etc/frr "
            "frrouting/frr:v7.5.1" % frr_volume_dir, echo=True)

    peer_address = run('docker inspect -f "{{ '
            'range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" frr', echo=True)
    with open("%s/config.yaml.tmpl" % dev_env_dir, 'r') as f:
        mlb_config = "# THIS FILE IS AUTOGENERATED\n" + f.read()
    mlb_config = mlb_config.replace("PEER_ADDRESS", peer_address.stdout.strip())
    with open("%s/config.yaml" % dev_env_dir, 'w') as f:
        f.write(mlb_config)
    # Apply the MetalLB ConfigMap
    run("kubectl apply -f %s/config.yaml" % dev_env_dir)


@task(help={
    "name": "name of the kind cluster to delete.",
})
def dev_env_cleanup(ctx, name="kind"):
    """Remove traces of the dev env."""
    validate_kind_version()
    clusters = run("kind get clusters", hide=True).stdout.strip().splitlines()
    if name in clusters:
        run("kind delete cluster --name={}".format(name), hide=True)
    else:
        raise Exit(message="Unable to find cluster named: {}".format(name))

    run('for frr in $(docker ps -a -f name=frr --format {{.Names}}) ; do '
        '    docker rm -f $frr ; '
        'done', hide=True)

    # cleanup bgp configs
    dev_env_dir = os.getcwd() + "/dev-env/bgp"
    frr_volume_dir = dev_env_dir + "/frr-volume"

    # sudo because past docker runs will have changed ownership of this dir
    run('sudo rm -rf "%s"' % frr_volume_dir)
    run('rm -f "%s"/config.yaml' % dev_env_dir)

    # cleanup layer2 configs
    dev_env_dir = os.getcwd() + "/dev-env/layer2"
    run('rm -f "%s"/config.yaml' % dev_env_dir)


@task
def test_cni_manifests(ctx):
    """Update CNI manifests for e2e tests."""
    def _fetch(url):
        bs = urlopen(url).read()
        return list(m for m in yaml.safe_load_all(bs) if m)
    def _write(file, manifest):
        with open(file, "w") as f:
            f.write(yaml.dump_all(manifest))

    calico = _fetch("https://docs.projectcalico.org/v3.6/getting-started/kubernetes/installation/hosted/kubernetes-datastore/calico-networking/1.7/calico.yaml")
    for manifest in calico:
        if manifest["kind"] != "DaemonSet":
            continue
        manifest["spec"]["template"]["spec"]["containers"][0]["env"].append({
            "name": "FELIX_IGNORELOOSERPF",
            "value": "true",
        })
    _write("e2etest/manifests/calico.yaml", calico)

    weave = _fetch("https://cloud.weave.works/k8s/net?k8s-version=1.15&env.NO_MASQ_LOCAL=1")
    _write("e2etest/manifests/weave.yaml", weave)

    flannel = _fetch("https://raw.githubusercontent.com/coreos/flannel/master/Documentation/kube-flannel.yml")
    _write("e2etest/manifests/flannel.yaml", flannel)


@task(help={
    "version": "version of MetalLB to release.",
    "skip-release-notes": "make the release even if there are no release notes.",
})
def release(ctx, version, skip_release_notes=False):
    """Tag a new release."""
    status = run("git status --porcelain", hide=True).stdout.strip()
    if status != "":
        raise Exit(message="git checkout not clean, cannot release")

    version = semver.parse_version_info(version)
    is_patch_release = version.patch != 0

    # Check that we have release notes for the desired version.
    run("git checkout main", echo=True)
    if not skip_release_notes:
        with open("website/content/release-notes/_index.md") as release_notes:
            if "## Version {}".format(version) not in release_notes.read():
                raise Exit(message="no release notes for v{}".format(version))

    # Move HEAD to the correct release branch - either a new one, or
    # an existing one.
    if is_patch_release:
        run("git checkout v{}.{}".format(version.major, version.minor), echo=True)
    else:
        run("git checkout -b v{}.{}".format(version.major, version.minor), echo=True)

    # Copy over release notes from main.
    if not skip_release_notes:
        run("git checkout main -- website/content/release-notes/_index.md", echo=True)

    # Update links on the website to point to files at the version
    # we're creating.
    if is_patch_release:
        previous_version = "v{}.{}.{}".format(version.major, version.minor, version.patch-1)
    else:
        previous_version = "main"
    def _replace(pattern):
        oldpat = pattern.format(previous_version)
        newpat = pattern.format("v{}").format(version)
        run("perl -pi -e 's#{}#{}#g' website/content/*.md website/content/*/*.md".format(oldpat, newpat),
            echo=True)
    _replace("/metallb/metallb/{}")
    _replace("/metallb/metallb/tree/{}")
    _replace("/metallb/metallb/blob/{}")

    # Update the version listed on the website sidebar
    run("perl -pi -e 's/MetalLB .*/MetalLB v{}/g' website/content/_header.md".format(version), echo=True)

    # Update the manifests with the new version
    run("perl -pi -e 's,image: metallb/speaker:.*,image: metallb/speaker:v{},g' manifests/metallb.yaml".format(version), echo=True)
    run("perl -pi -e 's,image: metallb/controller:.*,image: metallb/controller:v{},g' manifests/metallb.yaml".format(version), echo=True)

    # Update the versions in the helm chart (version and appVersion are always the same)
    # helm chart versions follow Semantic Versioning, and thus exclude the leading 'v'
    run("perl -pi -e 's,^version: .*,version: {},g' charts/metallb/Chart.yaml".format(version), echo=True)
    run("perl -pi -e 's,^appVersion: .*,appVersion: v{},g' charts/metallb/Chart.yaml".format(version), echo=True)
    run("perl -pi -e 's,^Current chart version is: .*,Current chart version is: `{}`,g' charts/metallb/README.md".format(version), echo=True)

    # Update the version in kustomize instructions
    #
    # TODO: Check if kustomize instructions really need the version in the
    # website or if there is a simpler way. For now, though, we just replace the
    # only page that mentions the version on release.
    run("perl -pi -e 's,github.com/metallb/metallb//manifests\?ref=.*,github.com/metallb/metallb//manifests\?ref=v{},g' website/content/installation/_index.md".format(version), echo=True)

    # Update the version embedded in the binary
    run("perl -pi -e 's/version\s+=.*/version = \"{}\"/g' internal/version/version.go".format(version), echo=True)
    run("gofmt -w internal/version/version.go", echo=True)

    run("git commit -a -m 'Automated update for release v{}'".format(version), echo=True)
    run("git tag v{} -m 'See the release notes for details:\n\nhttps://metallb.universe.tf/release-notes/#version-{}-{}-{}'".format(version, version.major, version.minor, version.patch), echo=True)
    run("git checkout main", echo=True)


@task
def test(ctx):
    """Run unit tests."""
    run("go test -short ./...")
    run("go test -short -race ./...")


@task
def checkpatch(ctx):
    # Generate a diff of all changes on this branch from origin/main
    # and look for any added lines with 2 spaces after a period.
    try:
        lines = run("git diff $(diff -u <(git rev-list --first-parent HEAD) "
                                " <(git rev-list --first-parent origin/main) "
                                " | sed -ne 's/^ //p' | head -1)..HEAD | "
                                " grep '+.*\.\  '")

        if len(lines.stdout.strip()) > 0:
            raise Exit(message="ERROR: Found changed lines with 2 spaces "
                               "after a period.")
    except UnexpectedExit:
        # Will exit non-zero if no double-space-after-period lines are found.
        pass


@task(help={
    "env": "Specify in which environment to run the linter . Default 'container'. Supported: 'container','host'"
})
def lint(ctx, env="container"):
    """Run linter.

    By default, this will run a golangci-lint docker image against the code.
    However, in some environments (such as the MetalLB CI), it may be more
    convenient to install the golangci-lint binaries on the host. This can be
    achieved by running `inv lint --env host`.
    """
    version = "1.39.0"

    if env == "container":
        run("docker run --rm -v $(git rev-parse --show-toplevel):/app -w /app golangci/golangci-lint:v{} golangci-lint run $(git rev-parse --show-toplevel)/...".format(version), echo=True)
    elif env == "host":
        run("curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b $(go env GOPATH)/bin v{}".format(version))
        run("golangci-lint run --timeout 5m0s $(git rev-parse --show-toplevel)/...")
    else:
        raise Exit(message="Unsupported linter environment: {}". format(env))


@task(help={
    "name": "name of the kind cluster to test.",
})
def e2etest(ctx, name="kind"):
    """Run E2E tests against development cluster."""
    validate_kind_version()
    clusters = run("kind get clusters", hide=True).stdout.strip().splitlines()
    if name in clusters:
        kubeconfig = tempfile.NamedTemporaryFile()
        run("kind export kubeconfig --name={} --kubeconfig={}".format(name, kubeconfig.name), pty=True, echo=True)
    else:
        raise Exit(message="Unable to find cluster named: {}".format(name))

    namespaces = ["kube-system", "metallb-system"]
    for ns in namespaces:
        run("kubectl -n {} wait --for=condition=Ready --all pods --timeout 300s".format(ns), hide=True)

    metallb_config = yaml.load(run("kubectl get configmaps -n metallb-system config -o jsonpath='{.data.config}'", hide=True).stdout)
    if metallb_config['address-pools'][0]['name'] == "dev-env-layer2":
        run("cd `git rev-parse --show-toplevel`/e2etest &&"
            "go test --provider=local -ginkgo.focus=L2 --kubeconfig={}".format(kubeconfig.name))
    elif metallb_config['address-pools'][0]['name'] == "dev-env-bgp":
        run("cd `git rev-parse --show-toplevel`/e2etest &&"
            "go test --provider=local -ginkgo.focus=BGP --kubeconfig={}".format(kubeconfig.name))
    else:
        print("dev-env environment not configured. Try running `inv dev-env -p <layer2/bgp>`")
