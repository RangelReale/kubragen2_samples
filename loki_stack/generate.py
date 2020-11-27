import argparse
import datetime
import os

from helmion.chart import ProcessorChain, Chart
from helmion.helmchart import HelmRequest
from helmion.processor import DefaultProcessor, FilterRemoveHelmData, ListSplitter
from helmion.resource import is_any_resource
from kubragen2.build import BuildData
from kubragen2.data import ValueData
from kubragen2.kdata import KData_PersistentVolume_HostPath, KData_PersistentVolumeClaim
from kubragen2.output import OutputProject, OutputFile_ShellScript, OutputFile_Kubernetes, OD_FileTemplate, \
    OutputDriver_Directory
from kubragen2.provider.aws import KData_PersistentVolume_CSI_AWSEBS
from kubragen2.provider.digitalocean import KData_PersistentVolume_CSI_DOBS
from kubragen2.provider.gcloud import KData_PersistentVolume_GCEPersistentDisk
from kubragen2.provider.local import KData_PersistentVolumeClaim_NoSelector


def main():
    parser = argparse.ArgumentParser(description='Kube Creator')
    parser.add_argument('-p', '--provider', help='provider', required=True, choices=[
        'google-gke',
        'amazon-eks',
        'digitalocean-kubernetes',
        'k3d',
    ])
    parser.add_argument('--no-resource-limit', help='don''t limit resources', action='store_true')
    parser.add_argument('-o', '--output-path', help='output path', default='output')
    args = parser.parse_args()

    pv_loki_files = None
    pvc_loki_files = None

    pvconfig = {
        'metadata': {
            'labels': {
                'pv.role': 'loki',
            },
        },
        'spec': {
            'persistentVolumeReclaimPolicy': 'Retain',
            'capacity': {
                'storage': '50Gi'
            },
            'accessModes': ['ReadWriteOnce'],
        },
    }

    pvcconfig = {
        'spec': {
            'selector': {
                'matchLabels': {
                    'pv.role': 'loki',
                }
            },
            'accessModes': ['ReadWriteOnce'],
            'resources': {
                'requests': {
                    'storage': '50Gi',
                }
            },
        }
    }

    if args.provider == 'k3d':
        pv_loki_files = KData_PersistentVolume_HostPath(
            name='loki-storage', hostpath={'path': '/var/storage/loki'}, merge_config=pvconfig)
        pvc_loki_files = KData_PersistentVolumeClaim_NoSelector(
            name='loki-claim', volumeName='loki-storage',
            namespace='monitoring', merge_config=pvcconfig)
    elif args.provider == 'google-gke':
        pv_loki_files = KData_PersistentVolume_GCEPersistentDisk(
            name='loki-storage', fsType='ext4',
            merge_config=pvconfig)
    elif args.provider == 'digitalocean-kubernetes':
        pv_loki_files = KData_PersistentVolume_CSI_DOBS(name='loki-storage', csi={
                'fsType': 'ext4',
            }, merge_config=pvconfig)
    elif args.provider == 'amazon-eks':
        pv_loki_files = KData_PersistentVolume_CSI_AWSEBS(name='loki-storage', csi={
                'fsType': 'ext4',
            }, merge_config=pvconfig)
    else:
        raise Exception('Unknown target')

    if pvc_loki_files is None:
        pvc_loki_files = KData_PersistentVolumeClaim(
            name='loki-claim', namespace='monitoring',
            storageclass='', merge_config=pvcconfig)

    # Add namespace to items, and filter Helm data from labels and annotations
    helm_default_processor = ProcessorChain(DefaultProcessor(add_namespace=True), FilterRemoveHelmData())

    def helm_splitter_crd(cat, chart, data):
            return is_any_resource(
                data, {'apiVersionNS': 'apiextensions.k8s.io', 'kind': 'CustomResourceDefinition'})

    def helm_splitter_config(cat, chart, data):
        return is_any_resource(
            data, {'apiVersionNS': 'rbac.authorization.k8s.io'},
            {'apiVersionNS': 'policy'},
            {'apiVersionNS': '', 'kind': 'ServiceAccount'},
            {'apiVersionNS': '', 'kind': 'Secret'},
            {'apiVersionNS': '', 'kind': 'ConfigMap'},
            {'apiVersionNS': 'monitoring.coreos.com'},
            {'apiVersionNS': 'admissionregistration.k8s.io'},
        )

    def helm_splitter_job(cat, chart, data):
        return is_any_resource(
            data, {'apiVersionNS': 'batch'})

    def helm_splitter_service(cat, chart, data):
        return is_any_resource(
            data, {'apiVersionNS': '', 'kind': 'Service'},
            {'apiVersionNS': '', 'kind': 'Pod'},
            {'apiVersionNS': '', 'kind': 'List'},
            {'apiVersionNS': 'apps', 'kind': 'Deployment'},
            {'apiVersionNS': 'apps', 'kind': 'DaemonSet'},
            {'apiVersionNS': 'apps', 'kind': 'StatefulSet'})

    # Start output
    out = OutputProject()

    shell_script = OutputFile_ShellScript('create_{}.sh'.format(args.provider))
    out.append(shell_script)

    shell_script.append('set -e')

    #
    # Provider setup
    #
    if args.provider == 'k3d':
        storage_directory = os.path.join(os.getcwd(), 'output', 'storage')
        if not os.path.exists(storage_directory):
            os.makedirs(storage_directory)
        if not os.path.exists(os.path.join(storage_directory, 'loki')):
            os.makedirs(os.path.join(storage_directory, 'loki'))
        shell_script.append(f'# k3d cluster create kg2sample-loki-stack --port 5051:80@loadbalancer --port 5052:443@loadbalancer -v {storage_directory}:/var/storage')

    #
    # OUTPUTFILE: namespace.yaml
    #
    file = OutputFile_Kubernetes('namespace.yaml')
    file.append([{
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': 'monitoring',
        },
    }])
    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: storage.yaml
    #
    file = OutputFile_Kubernetes('storage.yaml')

    file.append(pv_loki_files.get_value())
    file.append(pvc_loki_files.get_value())

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # HELM: traefik2
    #
    helmreq = HelmRequest(repository='https://helm.traefik.io/traefik', chart='traefik', version='9.11.0',
                          releasename='traefik-router', # rename to avoid conflict with k3d
                          namespace='monitoring', values=BuildData({
            'ingressRoute': {
                'dashboard': {
                    'enabled': False,
                }
            },
            'providers': {
                'kubernetesCRD': {
                    'enabled': True,
                    'namespaces': [
                        'default',
                        'monitoring',
                    ]
                },
                'kubernetesIngress': {
                    'enabled': False,
                }
            },
            'logs': {
                'access': {
                    'enabled': True,
                }
            },
            'globalArguments': [
                '--global.checkNewVersion=false',
                '--global.sendAnonymousUsage=false',
            ],
            'additionalArguments': [
                '--api.debug=true',
                '--api.dashboard=true',
                '--api.insecure=false',
            ],
            'ports': {
                'web': {
                    'expose': True,
                    'exposedPort': 80,
                },
                'websecure': {
                    'expose': False,
                },
                'api': {
                    'port': 8080,
                    'expose': True,
                },
            },
            'service': {
                'type': 'NodePort' if args.provider != 'k3d' else 'ClusterIP',
            },
            'resources': ValueData(value={
                'requests': {
                    'cpu': '100m',
                    'memory': '200Mi',
                },
                'limits': {
                    'cpu': '200m',
                    'memory': '300Mi',
                },
            }, enabled=not args.no_resource_limit),
        }))

    traefik_helmchart = helmreq.generate().process(helm_default_processor).split(
        ListSplitter({
            'crd': helm_splitter_crd,
            'config': helm_splitter_config,
            'service': helm_splitter_service,
        }, exactly_one_category=True))

    #
    # OUTPUTFILE: traefik-config-crd.yaml
    #
    file = OutputFile_Kubernetes('traefik-config-crd.yaml')

    file.append(traefik_helmchart['crd'].data)

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: traefik-config.yaml
    #
    file = OutputFile_Kubernetes('traefik-config.yaml')

    file.append(traefik_helmchart['config'].data)

    file.append([{
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'traefik-api',
            'namespace': 'monitoring',
        },
        'spec': {
            'entryPoints': ['api'],
            'routes': [{
                'match': 'Method(`GET`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'api@internal',
                    'kind': 'TraefikService'
                }]
            }]
        }
    }])

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # HELM: lokistack
    #
    helmreq = HelmRequest(repository='https://grafana.github.io/loki/charts', chart='loki-stack',
                          version='2.0.3', namespace='monitoring', values=BuildData({
            'loki': {
                'enabled': True,
                'ingress': {
                    'enabled': False,
                },
                'persistence': {
                    'enabled': True,
                    'existingClaim': 'loki-claim',
                },
                'service': {
                    'type': 'ClusterIP',
                    'port': 3100,
                },
                'resources': ValueData({
                    'requests': {
                        'cpu': '100m',
                        'memory': '128Mi'
                    },
                    'limits': {
                        'cpu': '200m',
                        'memory': '256Mi'
                    },
                }, enabled=not args.no_resource_limit)
            },
            'promtail': {
                'enabled': True,
                'resources': ValueData({
                    'requests': {
                        'cpu': '100m',
                        'memory': '128Mi'
                    },
                    'limits': {
                        'cpu': '200m',
                        'memory': '128Mi'
                    },
                }, enabled=not args.no_resource_limit),
            },
            'grafana': {
                'enabled': True,
                'adminPassword': 'grafana123',
                'resources': ValueData(value={
                    'requests': {
                        'cpu': '50m',
                        'memory': '100Mi'
                    },
                    'limits': {
                        'cpu': '100m',
                        'memory': '128Mi',
                    },
                }, enabled=not args.no_resource_limit),
            },
            'prometheus': {'enabled': False},
            'filebeat': {'enabled': False},
            'fluent-bit': {'enabled': False},
            'logstash': {'enabled': False},
        }))

    lokistack_helmchart = helmreq.generate().process(helm_default_processor).split(
        ListSplitter({
            'config': helm_splitter_config,
            'service': helm_splitter_service,
        }, exactly_one_category=True))

    #
    # OUTPUTFILE: lokistack-config.yaml
    #
    file = OutputFile_Kubernetes('lokistack-config.yaml')

    file.append(lokistack_helmchart['config'].data)

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: lokistack.yaml
    #
    file = OutputFile_Kubernetes('lokistack.yaml')

    file.append(lokistack_helmchart['service'].data)

    file.append([{
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'admin-grafana',
            'namespace': 'monitoring',
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                'match': f'Host(`admin-grafana.localdomain`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'loki-stack-grafana',
                    'namespace': 'monitoring',
                    'port': 80,
                }],
            }]
        }
    }])

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: http-echo.yaml
    #
    file = OutputFile_Kubernetes('http-echo.yaml')
    out.append(file)

    file.append([{
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {
            'name': 'echo-deployment',
            'namespace': 'default',
            'labels': {
                'app': 'echo'
            }
        },
        'spec': {
            'replicas': 1,
            'selector': {
                'matchLabels': {
                    'app': 'echo'
                }
            },
            'template': {
                'metadata': {
                    'labels': {
                        'app': 'echo'
                    }
                },
                'spec': {
                    'containers': [{
                        'name': 'echo',
                        'image': 'mendhak/http-https-echo',
                        'ports': [{
                            'containerPort': 80
                        },
                        {
                            'containerPort': 443
                        }],
                    }]
                }
            }
        }
    },
    {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {
            'name': 'echo-service',
            'namespace': 'default',
        },
        'spec': {
            'selector': {
                'app': 'echo'
            },
            'ports': [{
                'name': 'http',
                'port': 80,
                'targetPort': 80,
                'protocol': 'TCP'
            }]
        }
    }, {
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'http-echo',
            'namespace': 'default',
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                # 'match': f'Host(`http-echo.localdomain`)',
                'match': f'PathPrefix(`/`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'echo-service',
                    'port': 80,
                }],
            }]
        }
    }])

    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: traefik.yaml
    #
    file = OutputFile_Kubernetes('traefik.yaml')

    file.append(traefik_helmchart['service'].data)

    file.append({
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'admin-traefik',
            'namespace': 'monitoring',
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                'match': f'Host(`admin-traefik.localdomain`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'traefik-router',
                    'port': 8080,
                }],
            }]
        }
    })

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: ingress.yaml
    #
    file = OutputFile_Kubernetes('ingress.yaml')
    http_path = '/'
    if args.provider != 'k3d':
        http_path = '/*'

    ingress_chart = Chart(data=[
        {
            'apiVersion': 'extensions/v1beta1',
            'kind': 'Ingress',
            'metadata': {
                'name': 'ingress',
                'namespace': 'monitoring',
            },
            'spec': {
                'rules': [{
                    'http': {
                        'paths': [{
                            'path': http_path,
                            'backend': {
                                'serviceName': 'traefik-router',
                                'servicePort': 80,
                            }
                        }]
                    }
                }]
            }
        },
    ])

    if args.provider == 'amazon-eks':
        ingress_chart = ingress_chart.process(DefaultProcessor(jsonpatches=[{
            'condition': [
                {'op': 'check', 'path': '/kind', 'cmp': 'equals', 'value': 'Ingress'},
            ],
            'patch': [
                {'op': 'merge', 'path': '/metadata', 'value': {'annotations': {
                    'kubernetes.io/ingress.class': 'alb',
                    'alb.ingress.kubernetes.io/scheme': 'internet-facing',
                    'alb.ingress.kubernetes.io/listen-ports': '[{"HTTP": 80}]',
                }}}
            ]
        }]))

    file.append(ingress_chart.data)

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUT
    #
    output_path = os.path.join(args.output_path, '{}-{}'.format(
        args.provider, datetime.datetime.today().strftime("%Y%m%d-%H%M%S")))
    print('Saving files to {}'.format(output_path))
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    out.output(OutputDriver_Directory(output_path))


if __name__ == "__main__":
    main()
