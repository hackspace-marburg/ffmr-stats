#!/usr/bin/env python3

from influxdb import InfluxDBClient
from influxdb.client import InfluxDBClientError
import click
import requests
import geohash

def get_api_resource(url: str) -> dict:
    try:
        r = requests.get(url)
        api = r.json()
        assert api['version'] == 1
    except requests.exceptions.RequestException as e:
        print(e)
        exit(1)

    return api

def get_nodes(url: str) -> dict:
    api = get_api_resource(url)

    return api['nodes']

def get_graph(url: str) -> dict:
    api = get_api_resource(url)

    return api['batadv']

def flatten_statistics(stats: dict) -> dict:
    def items():
        for k, v in stats.items():
            if isinstance(v, dict):
                for child_k, child_v in flatten_statistics(v).items():
                    yield k + '_' + child_k, child_v
            else:
                yield k, v

    return dict(items())

def generate_statistics_query(stats: dict, timestamp: str, tags: dict) -> dict:
    measurements = []
    for k, v in flatten_statistics(stats).items():
        if k in ['gateway']:
          value = str(v)
        elif k in ['uptime', 'loadavg', 'memory_usage', 'rootfs_usage']:
          value = float(v)
        else:
          value = int(v)
        measurement = {
            'measurement': k,
            'tags': tags,
            'time': timestamp,
            'fields': {
                'value': value
            }
        }
        measurements.append(measurement)

    return measurements

def get_nodes_for_link(graph_nodes: dict, real_nodes: dict, source: int, target: int) -> tuple:
    s_nodeid = graph_nodes[source]['node_id']
    t_nodeid = graph_nodes[target]['node_id']

    return (
        s_nodeid,
        t_nodeid,
        real_nodes[s_nodeid]['nodeinfo']['hostname'],
        real_nodes[t_nodeid]['nodeinfo']['hostname']
    )

@click.group()
def cli():
    '''Export data from ffmap-backend nodes.json to InfluxDB'''
    pass

@cli.command()
@click.option('--db_host', 
              default='localhost',
              help='InfluxDB host')
@click.option('--db_port', 
              default=8086,
              help='InfluxDB port')
@click.option('--db_user', 
              default='ffmr',
              help='Únprivileged InfluxDB user')
@click.option('--db_password', 
              default='ffmr',
              help='Únprivileged InfluxDB password')
@click.option('--db_name', 
              default='ffmr',
              help='InfluxDB database')
@click.option('--admin_user', 
              default='root',
              help='Administrative InfluxDB user')
@click.option('--admin_password', 
              default='root',
              help='Administrative InfluxDB password')
def setup_db(db_host: str, db_port: int,
             db_user: str, db_password: str, db_name: str,
             admin_user: str, admin_password: str):
    '''Setup unprivileged user, database and retention policy'''
    db = InfluxDBClient(db_host, db_port, admin_user, admin_password, db_name)
    print('Creating database: ' + db_name)
    try:
        db.create_database(db_name)
    except InfluxDBClientError:
        print('Database does already exist. Skipping creation.')
        pass

    print('Creating retention policy: ' + db_name)
    try:
        db.create_retention_policy(db_name, 'INF', 1, database=db_name, default=True)
    except InfluxDBClientError:
        print('Retention policy does already exist. Skipping creation.')
        pass

    print('Creating unprivileged user: ' + db_user)
    try:
        db.create_user(db_user, db_password, admin=False)
    except InfluxDBClientError:
        print('User does already exist. Skipping creation.')
        pass

    print('Granting privileges on ' + db_name + ' to unprivileged user: ' + db_user)
    try:
        db.grant_privilege('all', db_name, db_user)
    except InfluxDBClientError as e:
        print(e)
        pass

@cli.command()
@click.option('--db_host', 
              default='localhost',
              help='InfluxDB Host')
@click.option('--db_port', 
              default=8086,
              help='InfluxDB Port')
@click.option('--db_user', 
              default='ffmr',
              help='Únprivileged InfluxDB User')
@click.option('--db_password', 
              default='ffmr',
              help='Únprivileged InfluxDB Password')
@click.option('--db_name', 
              default='ffmr',
              help='InfluxDB Database')
@click.option('--nodes_url', 
              default='https://api.marburg.freifunk.net/nodes.json',
              help='URL of nodes.json generated by ffmap-backend')
@click.option('--graph_url', 
              default='https://api.marburg.freifunk.net/graph.json',
              help='URL of graph.json generated by ffmap-backend')
def insert_data(db_host: str, db_port: int,
                db_user: str, db_password: str, db_name: str,
                nodes_url: str, graph_url: str):
    '''Fetch data from nodes.json as well as graph.json and export it to InfluxDB'''
    db = InfluxDBClient(db_host, db_port, db_user, db_password, db_name)

    nodes = get_nodes(nodes_url)
    for k, v in nodes.items():
        try:
          if 'role' in v['nodeinfo']['system']:
              if v['nodeinfo']['system']['role'] == 'gateway':
                  continue
          tags = {
              'node_id': v['nodeinfo']['node_id'],
              'hostname': v['nodeinfo']['hostname'],
              'firmware_base': v['nodeinfo']['software']['firmware']['base'],
              'firmware_release': v['nodeinfo']['software']['firmware']['release'],
              'autoupdater_enabled': v['nodeinfo']['software']['autoupdater']['enabled'],
              'autoupdater_branch': v['nodeinfo']['software']['autoupdater']['branch'],
              'hardware_model': v['nodeinfo']['hardware']['model'],
              'hardware_nproc': v['nodeinfo']['hardware']['nproc']
          }
          # location is optional
          try:
              tags['location'] = geohash.encode(
                  v['nodeinfo']['location']['latitude'],
                  v['nodeinfo']['location']['longitude']
              )
          except:
              pass
          db.write_points(generate_statistics_query(v['statistics'], v['lastseen'], tags))
        except:
          continue  # ignore failing nodes

    graph = get_graph(graph_url)
    for link in graph['links']:
        try:
            s_nodeid, t_nodeid, s_hostname, t_hostname = get_nodes_for_link(
                graph['nodes'], nodes,
                link['source'], link['target']
            )
            measurement = [
                {
                    'measurement': 'link',
                    'time_precision': 'm',
                    'tags': {
                        's_nodeid': s_nodeid,
                        't_nodeid': t_nodeid,
                        's_hostname': s_hostname,
                        't_hostname': t_hostname,
                        'bidirect': link['bidirect'],
                        'vpn': link['vpn']
                    },
                    'fields': {
                        'tq': float(1 / link['tq'])  # TQ 0 - 255 becomes 0.0 ... 1.0
                    }
                }
            ]
            db.write_points(measurement)
        except:
            continue  # ignore failing nodes


if __name__ == '__main__':
    cli()
