#!/usr/bin/env python
# encoding: utf-8

from __future__ import (absolute_import, division, print_function, unicode_literals)
import logging
import numpy as np
import os

from arkane.statmech import Log

from arc.exceptions import InputError

"""
Various ESS parsing tools
"""

##################################################################


def parse_frequencies(path, software):
    if not os.path.isfile(path):
        raise InputError('Could not find file {0}'.format(path))
    freqs = np.array([], np.float64)
    if software.lower() == 'qchem':
        with open(path, 'rb') as f:
            for line in f:
                if ' Frequency:' in line:
                    items = line.split()
                    for i, item in enumerate(items):
                        if i:
                            freqs = np.append(freqs, [(float(item))])
    elif software.lower() == 'gaussian':
        with open(path, 'rb') as f:
            line = f.readline()
            while line != '':
                if 'Frequencies --' in line:
                    freqs = np.append(freqs, [float(frq) for frq in line.split()[2:]])
                line = f.readline()
    else:
        raise ValueError('parse_frequencies() can curtrently only parse QChem and gaussian files,'
                         ' got {0}'.format(software))
    logging.debug('Using parser.parse_frequencies. Determined frequencies are: {0}'.format(freqs))
    return freqs


def parse_t1(path):
    """
    Parse the T1 parameter from a Molpro coupled cluster calculation
    """
    if not os.path.isfile(path):
        raise InputError('Could not find file {0}'.format(path))
    t1 = None
    with open(path, 'rb') as f:
        for line in f:
            if 'T1 diagnostic:' in line:
                t1 = float(line.split()[-1])
    return t1


def parse_e0(path):
    """
    Parse the zero K energy, E0, from an sp job
    """
    if not os.path.isfile(path):
        raise InputError('Could not find file {0}'.format(path))
    log = Log(path='')
    log.determine_qm_software(fullpath=path)
    try:
        e0 = log.loadEnergy(frequencyScaleFactor=1.) * 0.001  # convert to kJ/mol
    except Exception:
        e0 = None
    return e0

def parse_lines(path,start,stop=None,time=1):
    """
    Return the list of lines from the time-th occurance of start
    to the next occurance of stop
    """
    if not os.path.isfile(path):
        raise InputError('Could not find file {0}'.format(path))
    f = open(path)
    line = f.readline()
    lines = []
    boo = False
    c = 1
    while line != '':
        if start in line:
            if time == c:
                boo = True
                line = f.readline()
                continue
            else:
                c += 1

        if boo and stop in line:
            break
        if boo:
            lines.append(line)
        line = f.readline()
    return lines

def parse_scan_coords(path,point_index,software):
    """
    Parse the coordinates of the point_index-th point
    in a rotor scan
    """
    if software == 'gaussian':
        start = 'Optimization completed'
        stop = 'Distance matrix (angstroms):'
        lines = parse_lines(path,start,stop=stop,time=point_index+1)
        atoms = int(lines[-2].split()[0])
        lines = lines[-atoms-1:-1]
        coords = []
        for line in lines:
            vals = line.split()
            coords.append([float(x) for x in vals[3:]])
        coords = np.array(coords)
        return coords
    else:
        raise ValueError('parse_scan_coords() can currently only parse gaussian files,'
                         ' got {0}'.format(software))
