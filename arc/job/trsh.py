#!/usr/bin/env python3
# encoding: utf-8

"""
The ARC troubleshooting ("trsh") module
"""

import math
import os

import numpy as np

from arc.common import get_logger, determine_ess
from arc.exceptions import SpeciesError, TrshError
from arc.job.local import execute_command
from arc.job.ssh import SSHClient
from arc.settings import (delete_command,
                          inconsistency_ab,
                          inconsistency_az,
                          list_available_nodes_command,
                          maximum_barrier,
                          preserve_param_in_scan_ratio,
                          rotor_scan_resolution,
                          servers,
                          submit_filename)
from arc.species import ARCSpecies
from arc.species.conformers import determine_smallest_atom_index_in_scan
from arc.species.converter import xyz_from_data, xyz_to_coords_list
from arc.species.species import determine_rotor_symmetry
from arc.species.vectors import calculate_dihedral_angle, calculate_distance
from arc.parser import parse_normal_displacement_modes, parse_xyz_from_file


logger = get_logger()


def determine_ess_status(output_path: str,
                         species_label: str,
                         job_type: str,
                         software: str = None):
    """
    Determine the reason that caused an ESS job to crash, assign error keywords for troubleshooting.

    Args:
        output_path (str): The path to the ESS output file.
        species_label (str): The species label.
        job_type (str): The job type (e.g., 'opt, 'freq', 'ts', 'sp').
        software (str, optional): The ESS software.

    Returns:
        status (str): The status. Either 'done' or 'errored'.
    Returns:
        keywords (list): The standardized error keywords.
    Returns:
        error (str): A description of the error.
    Returns:
        line (str): The parsed line from the ESS output file indicating the error.
    """
    if software is None:
        software = determine_ess(log_file=output_path)

    keywords, error, = list(), ''
    with open(output_path, 'r') as f:
        lines = f.readlines()
        if len(lines) < 5:
            return 'errored', ['NoOutput'], 'Log file could not be read', ''
        forward_lines = tuple(lines)
        reverse_lines = tuple(lines[::-1])

        if software == 'gaussian':
            for line in forward_lines[-1:-20:-1]:
                if 'Normal termination' in line:
                    return 'done', list(), '', ''
            for i, line in enumerate(reverse_lines):
                if 'termination' in line:
                    if 'l9999.exe' in line or 'link 9999' in line:
                        keywords = ['Unconverged', 'GL9999']  # GL stand for Gaussian Link
                        error = 'Unconverged'
                    elif 'l101.exe' in line:
                        keywords = ['InputError', 'GL101']
                        error = 'The blank line after the coordinate section is missing, ' \
                                'or charge/multiplicity was not specified correctly.'
                    elif 'l103.exe' in line:
                        keywords = ['InternalCoordinateError', 'GL103']
                        error = 'Internal coordinate error'
                    elif 'l108.exe' in line:
                        keywords = ['InputError', 'GL108']
                        error = 'There are two blank lines between z-matrix and ' \
                                'the variables, expected only one.'
                    elif 'l202.exe' in line:
                        keywords = ['OptOrientation', 'GL202']
                        error = 'During the optimization process, either the standard ' \
                                'orientation or the point group of the molecule has changed.'
                    elif 'l301.exe' in line:
                        keywords = ['GL301']
                    elif 'l401.exe' in line:
                        keywords = ['GL401']
                    elif 'l502.exe' in line:
                        keywords = ['SCF', 'GL502']
                        error = 'Unconverged SCF.'
                    elif 'l716.exe' in line:
                        keywords = ['ZMat', 'GL716']
                        error = 'Angle in z-matrix outside the allowed range 0 < x < 180.'
                    elif 'l906.exe' in line:
                        keywords = ['MP2', 'GL906']
                        error = 'The MP2 calculation has failed. It may be related to pseudopotential. ' \
                                'Basis sets (CEP-121G*) that are used with polarization functions, ' \
                                'where no polarization functions actually exist.'
                    elif 'l913.exe' in line:
                        keywords = ['MaxOptCycles', 'GL913']
                        error = 'Maximum optimization cycles reached.'
                    if any([keyword in ['GL301', 'GL401'] for keyword in keywords]):
                        additional_info = forward_lines[len(forward_lines) - i - 2]
                        if 'No data on chk file' in additional_info \
                                or 'Basis set data is not on the checkpoint file' in additional_info:
                            keywords = ['CheckFile']
                            error = additional_info.rstrip()
                        elif 'GL301' in keywords:
                            if 'Atomic number out of range for' in forward_lines[len(forward_lines) - i - 2]:
                                keywords.append('BasisSet')
                                error = f'The basis set {forward_lines[len(forward_lines) - i - 2].split()[6]} ' \
                                        f'is not appropriate for the this chemistry.'
                            else:
                                keywords.append('InputError')
                                error = 'Either charge, multiplicity, or basis set was not ' \
                                        'specified correctly. Alternatively, a specified atom does not match any ' \
                                        'standard atomic symbol.'
                        elif 'GL401' in keywords:
                            keywords.append('BasisSet')
                            error = 'The projection from the old to the new basis set has failed.'
                elif 'Erroneous write' in line or 'Write error in NtrExt1' in line:
                    keywords = ['DiskSpace']
                    error = 'Ran out of disk space.'
                    line = ''
                elif 'NtrErr' in line:
                    keywords = ['CheckFile']
                    error = 'An operation on the check file was specified, but a .chk was not found or is incomplete.'
                    line = ''
                elif 'malloc failed' in line or 'galloc' in line:
                    keywords = ['Memory']
                    error = 'Memory allocation failed (did you ask for too much?)'
                    line = ''
                elif 'PGFIO/stdio: No such file or directory' in line:
                    keywords = ['Scratch']
                    error = 'Wrongly specified the scratch directory. Correct the "GAUSS_SCRDIR" ' \
                            'variable in the submit script, it should point to an existing directory. ' \
                            'Make sure to add "mkdir -p $GAUSS_SCRDIR" to your submit script.'
                    line = ''
                if 'a syntax error was detected' in line.lower():
                    keywords = ['Syntax']
                    error = 'There was a syntax error in the Gaussian input file. Check your Gaussian input file ' \
                            'template under arc/job/inputs.py. Alternatively, perhaps the level of theory is not ' \
                            'supported by Gaussian in the format it was given.'
                    line = ''
                if keywords:
                    break
            error = error if error else 'Gaussian job terminated for an unknown reason. ' \
                                        'It is possible there was a server node failure.'
            keywords = keywords if keywords else ['Unknown']
            return 'errored', keywords, error, line

        elif software == 'qchem':
            done = False
            for line in reverse_lines:
                if 'Thank you very much for using Q-Chem' in line:
                    done = True
                    # if this is an opt job, we must also check that the max num of cycles hasn't been reached,
                    # so don't break yet
                    if 'opt' not in job_type and 'conformer' not in job_type and 'ts' not in job_type:
                        break
                elif 'SCF failed' in line:
                    keywords = ['SCF']
                    error = 'SCF failed'
                    break
                elif 'error' in line and 'DIIS' not in line:
                    # these are **normal** lines that we should not capture:
                    # "SCF converges when DIIS error is below 1.0E-08", or
                    # "Cycle       Energy         DIIS Error"
                    keywords = ['SCF', 'DIIS']
                    error = 'SCF failed'
                    break
                elif 'Invalid charge/multiplicity combination' in line:
                    raise SpeciesError(f'The multiplicity and charge combination for species '
                                       f'{species_label} are wrong.')
                if 'opt' in job_type or 'conformer' in job_type or 'ts' in job_type:
                    if 'MAXIMUM OPTIMIZATION CYCLES REACHED' in line:
                        keywords = ['MaxOptCycles']
                        error = 'Maximum optimization cycles reached.'
                        break
                    elif 'OPTIMIZATION CONVERGED' in line and done:  # `done` should already be assigned
                        done = True
                        break
            if done:
                return 'done', keywords, '', ''
            error = error if error else 'QChem job terminated for an unknown reason.'
            keywords = keywords if keywords else ['Unknown']
            return 'errored', keywords, error, line

        elif software == 'orca':
            done = False
            for i, line in enumerate(reverse_lines):
                if 'ORCA TERMINATED NORMALLY' in line:
                    # not done yet, things can still go wrong (e.g., SCF energy might blow up)
                    for j, info in enumerate(forward_lines):
                        if 'Starting incremental Fock matrix formation' in info:
                            scf_energy_initial_iteration = float(forward_lines[j + 1].split()[1])
                        if 'TOTAL SCF ENERGY' in info:
                            # this value is very close to the scf energy at last iteration and is easier to parse
                            scf_energy_last_iteration = float(forward_lines[j + 3].split()[3])
                            break
                    # Check if final SCF energy makes sense
                    scf_energy_ratio = scf_energy_last_iteration / scf_energy_initial_iteration
                    scf_energy_ratio_threshold = 2  # it is rare that this ratio > 2
                    if scf_energy_ratio > scf_energy_ratio_threshold:
                        keywords = ['SCF']
                        error = f'The SCF energy seems diverged during iterations. SCF energy after initial ' \
                                f'iteration is {scf_energy_initial_iteration}. SCF energy after final iteration ' \
                                f'is {scf_energy_last_iteration}. The ratio between final and initial SCF energy ' \
                                f'is {scf_energy_ratio}. This ratio is greater than the default threshold of ' \
                                f'{scf_energy_ratio_threshold}. Please consider using alternative methods or larger ' \
                                f'basis sets.'
                        line = ''
                    else:
                        done = True
                    break
                elif 'ORCA finished by error termination in SCF' in line:
                    keywords = ['SCF']
                    for j, info in enumerate(reverse_lines):
                        if 'Please increase MaxCore' in info:
                            estimated_mem = info.split()[-2]  # e.g., Please increase MaxCore to more than: 289 MB
                            keywords.append('Memory')
                            line = reverse_lines[j + 3].rstrip()  # e.g., Error (ORCA_SCF): Not enough memory available!
                            error = f'Orca suggests to increase per cpu core memory to {estimated_mem} MB.'
                            break
                    else:
                        error = f'SCF error in Orca.'
                    break
                elif 'ORCA finished by error termination in MDCI' in line:
                    keywords = ['MDCI']
                    for j, info in enumerate(reverse_lines):
                        if 'Please increase MaxCore' in info:
                            # e.g., Please increase MaxCore - by at least ( 9717.9 MB)
                            # This message appears multiple times, and suggest different memory at each appearance
                            # Need to store all suggested memory values, and then pick the largest one
                            estimated_mem_list = []
                            for message in reverse_lines:
                                if 'Please increase MaxCore' in message:
                                    estimated_mem = math.ceil(float(message.split()[-2]))
                                    estimated_mem_list.append(estimated_mem)
                            keywords.append('Memory')
                            estimated_max_mem = np.max(estimated_mem_list)
                            error = f'Orca suggests to increase per cpu core memory to {estimated_max_mem} MB.'
                            line = info
                            break
                        elif 'parallel calculation exceeds number of pairs' in info:
                            # e.g., Error (ORCA_MDCI): Number of processes (16) in parallel calculation exceeds
                            # number of pairs (10)
                            max_core = int(info.split()[-1].strip('()'))
                            keywords.append('cpu')
                            error = f'Orca cannot utilize cpu cores more than electron pairs in a molecule. The ' \
                                    f'maximum number of cpu cores can be used for this job is {max_core}.'
                            line = info
                            break
                    else:
                        error = f'MDCI error in Orca.'
                    break
                elif 'Error : multiplicity' in line:
                    keywords = ['Input']
                    error = f'The multiplicity and charge combination for species {species_label} are wrong.'
                    break
                elif 'UNRECOGNIZED OR DUPLICATED KEYWORD' in line:
                    # e.g., UNRECOGNIZED OR DUPLICATED KEYWORD(S) IN SIMPLE INPUT LINE
                    keywords = ['Syntax']
                    line = reverse_lines[i - 1]  # this line in the log file suggests which keyword might be problematic
                    problematic_keyword = line.split()[0]
                    error = f'There was keyword syntax error in the Orca input file. In particular, keywords ' \
                            f'{problematic_keyword} can either be duplicated or illegal. Please check your Orca ' \
                            f'input file template under arc/job/inputs.py. Alternatively, perhaps the level of ' \
                            f'theory or the job option is not supported by Orca in the format it was given.'
                    break
                elif 'There are no CABS' in line:
                    # e.g., ** There are no CABS   basis functions on atom number   2 (Br) **
                    keywords = ['Basis']
                    problematic_atom = line.split()[-2].strip('()')
                    error = f'There was a basis set error in the Orca input file. In particular, basis for atom type ' \
                            f'{problematic_atom} is missing. Please check if specified basis set supports this atom.'
                    break
                elif 'This wavefunction IS NOT FULLY CONVERGED!' in line:
                    keywords = ['Convergence']
                    error = f'Specified wavefunction method is not converged. Please restart calculation with larger ' \
                            f'max iterations or with different convergence flags.'
                    break
            if done:
                return 'done', keywords, '', ''
            error = error if error else 'Orca job terminated for an unknown reason.'
            keywords = keywords if keywords else ['Unknown']
            return 'errored', keywords, error, line

        elif software == 'molpro':
            for line in reverse_lines:
                if 'molpro calculation terminated' in line.lower() \
                        or 'variable memory released' in line.lower():
                    return 'done', list(), '', ''
                elif 'No convergence' in line:
                    keywords = ['Unconverged']
                    error = 'Unconverged'
                    break
                elif 'A further' in line and 'Mwords of memory are needed' in line and 'Increase memory to' in line:
                    # e.g.: `A further 246.03 Mwords of memory are needed for the triples to run.
                    # Increase memory to 996.31 Mwords.` (w/o the line break)
                    keywords = ['Memory']
                    error = f'Additional memory required: {line.split()[2]} MW'
                    break
                elif 'insufficient memory available - require' in line:
                    # e.g.: `insufficient memory available - require              228765625  have
                    #        62928590
                    #        the request was for real words`
                    # add_mem = (float(line.split()[-2]) - float(prev_line.split()[0])) / 1e6
                    keywords = ['Memory']
                    error = f'Additional memory required: {float(line.split()[-2]) / 1e6} MW'
                    break
                elif 'Basis library exhausted' in line:
                    # e.g.:
                    # ` SETTING BASIS          =    6-311G**
                    #
                    #
                    #  Using spherical harmonics
                    #
                    #  LIBRARY EXHAUSTED
                    #   Searching for I  S 6-311G
                    #   Library contains the following bases:
                    #  ? Error
                    #  ? Basis library exhausted
                    #  ? The problem occurs in Binput`
                    keywords = ['BasisSet']
                    basis_set = None
                    for line0 in reverse_lines:
                        if 'SETTING BASIS' in line0:
                            basis_set = line0.split()[-1]
                    error = f'Unrecognized basis set {basis_set}'
                    break
                elif 'the problem occurs' in line:
                    keywords = ['Unknown']
                    error = 'Unknown'
                    break
            error = error if error else 'Molpro job terminated for an unknown reason.'
            keywords = keywords if keywords else ['Unknown']
            return 'errored', keywords, error, line

        elif software == 'terachem':
            for line in lines[::-1]:
                if 'Job finished:' in line:
                    return 'done', list(), '', ''
                elif 'incorrect method' in line.lower():
                    keywords = ['IncorrectMethod']
                    error = 'incorrect method'
                    break
                elif 'error: ' in line.lower():
                    # e.g.: "ERROR: Closed shell calculations can't have spin multiplicity 0."
                    keywords = ['Unknown']  # Todo
                    error = line.split()[1]
                    break
                elif 'unable to open file: ' in line.lower() and 'basis' in line.lower():
                    # e.g.: "Unable to open file /<..path..>/TeraChem/basis/6-311++g[d,p]"
                    keywords = ['MissingBasisSet']
                    error = 'Could not find basis set {0} in TeraChem'.format(
                             line.split('/')[-1].replace('[', '(').replace(']', ')'))
            error = error if error else 'TeraChem job terminated for an unknown reason.'
            keywords = keywords if keywords else ['Unknown']
            return 'errored', keywords, error, line


def trsh_negative_freq(label: str,
                       log_file: str,
                       neg_freqs_trshed: list = None,
                       job_types: list = None):
    """
    Troubleshooting cases where non-TS species have negative frequencies.
    We take +/-1.1 displacements, generating several new initial geometries.

    Args:
        label (str): The species label.
        log_file (str): The frequency job log file.
        neg_freqs_trshed (list, optional): A list of negative frequencies the species was troubleshooted for.
        job_types (list, optional): The job types used for ARC, e.g., ['opt', 'rotors'].

    Todo:
        * get all torsions of the molecule (if weren't already generated),
          identify atom/s with largest displacements (top 2)
          determine torsions with unique PIVOTS where these atoms are in the "scan" and "top" but not pivotal
          generate a 360 scan using 30 deg increments and append all 12 results as conformers
          (consider rotor symmetry to append less conformers?)

    Returns:
        current_neg_freqs_trshed (list): The current troubleshooted negative frequencies.
    Returns:
        conformers (list): The new conformers to try optimizing.
    Returns:
        output_errors (list): Errors to report.
    Returns:
        output_warnings (list): Warnings to report.

    Raises:
        TrshError: If a negative frequency could not be determined.
    """
    neg_freqs_trshed = neg_freqs_trshed if neg_freqs_trshed is not None else list()
    job_types = job_types if job_types is not None else ['rotors']
    output_errors, output_warnings, conformers, current_neg_freqs_trshed = list(), list(), list(), list()
    factor = 1.1
    try:
        freqs, normal_disp_modes = parse_normal_displacement_modes(path=log_file)
    except NotImplementedError as e:
        logger.error(f'Could not troubleshoot negative frequency for species {label}, got:\n{e}')
        return [], [], output_errors, []
    if len(neg_freqs_trshed) > 10:
        logger.error(f'Species {label} was troubleshooted for negative frequencies too many times.')
        if 'rotors' not in job_types:
            logger.error('The rotor scans feature is turned off, '
                         'cannot troubleshoot geometry using dihedral modifications.')
            output_warnings.append('rotors = False; ')
        logger.error('Invalidating species.')
        output_errors.append('Error: Encountered negative frequencies too many times; ')
    else:
        neg_freqs_idx = list()  # store indices w.r.t. vibfreqs
        largest_neg_freq_idx = 0  # index in vibfreqs
        for i, freq in enumerate(freqs):
            if freq < 0:
                neg_freqs_idx.append(i)
                if freqs[i] < freqs[largest_neg_freq_idx]:
                    largest_neg_freq_idx = i
            else:
                # assuming frequencies are ordered, break after the first positive freq encountered
                break
        if freqs[largest_neg_freq_idx] >= 0 or len(neg_freqs_idx) == 0:
            raise TrshError(f'Could not determine a negative frequency for species {label} '
                            f'while troubleshooting for it.')
        if len(neg_freqs_idx) == 1 and not len(neg_freqs_trshed):
            # species has one negative frequency, and has not been troubleshooted for it before
            logger.info(f'Species {label} has a negative frequency ({freqs[largest_neg_freq_idx]}). Perturbing its '
                        f'geometry using the respective vibrational displacements')
            neg_freqs_idx = [largest_neg_freq_idx]  # indices of the negative frequencies to troubleshoot for
        elif len(neg_freqs_idx) == 1 and any([np.allclose(freqs[0], vf, rtol=1e-04, atol=1e-02)
                                              for vf in neg_freqs_trshed]):
            # species has one negative frequency, and has been troubleshooted for it before
            factor = 1 + 0.1 * (len(neg_freqs_trshed) + 1)
            logger.info(f'Species {label} has a negative frequency ({freqs[largest_neg_freq_idx]}) for the '
                        f'{len(neg_freqs_trshed)} time. Perturbing its geometry using the respective vibrational '
                        f'displacements, this time using a larger factor (x {factor})')
            neg_freqs_idx = [largest_neg_freq_idx]  # indices of the negative frequencies to troubleshoot for
        elif len(neg_freqs_idx) > 1 and not any([np.allclose(freqs[0], vf, rtol=1e-04, atol=1e-02)
                                                 for vf in neg_freqs_trshed]):
            # species has more than one negative frequency, and has not been troubleshooted for it before
            logger.info(f'Species {label} has {len(neg_freqs_idx)} negative frequencies. Perturbing its geometry using the vibrational '
                        f'displacements of its largest negative frequency, {freqs[largest_neg_freq_idx]}')
            neg_freqs_idx = [largest_neg_freq_idx]  # indices of the negative frequencies to troubleshoot for
        elif len(neg_freqs_idx) > 1 and any([np.allclose(freqs[0], vf, rtol=1e-04, atol=1e-02)
                                             for vf in neg_freqs_trshed]):
            # species has more than one negative frequency, and has been troubleshooted for it before
            logger.info(f'Species {label} has {len(neg_freqs_idx)} negative frequencies. Perturbing its geometry '
                        f'using the vibrational displacements of ALL negative frequencies')
        # convert a numpy array to a list, imprtant for saving the neg_freqs_trshed species attribute in the restart
        freqs_list = freqs.tolist()
        current_neg_freqs_trshed = [round(freqs_list[i], 2) for i in neg_freqs_idx]  # record trshed negative freqs
        xyz = parse_xyz_from_file(log_file)
        coords = np.array(xyz_to_coords_list(xyz), np.float64)
        for neg_freq_idx in neg_freqs_idx:
            displacement = normal_disp_modes[neg_freq_idx]
            coords1 = coords + factor * displacement
            coords2 = coords - factor * displacement
            conformers.append(xyz_from_data(coords=coords1, symbols=xyz['symbols']))
            conformers.append(xyz_from_data(coords=coords2, symbols=xyz['symbols']))
    return current_neg_freqs_trshed, conformers, output_errors, output_warnings


def trsh_scan_job(label: str,
                  scan_res: int,
                  scan: list,
                  species_scan_lists: list,
                  methods: list):
    """
    Troubleshooting rotor scans
    Using the following methods: freezing all dihedrals other than the scan's pivots for this job,
    or increasing the scan resolution.

    Args:
        label (str): The species label.
        scan_res (int): The scan resolution in degrees.
        scan (list): The four atom indices representing the torsion to be troubleshooted.
        species_scan_lists (list): Entries are lists of four atom indices each representing a torsion.
        methods (list): The troubleshooting method/s to try. Accepted values: 'freeze' and/or 'inc_res'.

    Raises:
        TrshError: If troubleshooted dihedral is not found.

    Returns:
        scan_trsh (str): The scan troubleshooting keywords to be appended to the Gaussian input file.
    Returns:
        scan_res (int): The new scan resolution in degrees.
    """
    if methods is None:
        raise TrshError('Expected to get a list of methods, got None.')
    scan_trsh = ''
    if 'freeze' in methods:
        if scan not in species_scan_lists:
            raise TrshError(f'Could not find the dihedral to troubleshoot for in the scan list of species {label}')
        species_scan_lists.pop(species_scan_lists.index(scan))
        if len(species_scan_lists):
            scan_trsh = '\n'
            for scan in species_scan_lists:
                scan_trsh += 'D ' + ''.join([str(num) + ' ' for num in scan]) + 'F\n'
    if 'inc_res' in methods:
        scan_res = min(4, int(scan_res / 2))
        # make sure mod(360, scan res) is 0:
        if scan_res not in [4, 2, 1]:
            scan_res = min([4, 2, 1], key=lambda x: abs(x - scan_res))
    return scan_trsh, scan_res


def trsh_ess_job(label: str,
                 level_of_theory_dict: dict,
                 server: str,
                 job_status: dict,
                 job_type: str,
                 software: str,
                 fine: bool,
                 memory_gb: float,
                 num_heavy_atoms: int,
                 cpu_cores: int,
                 ess_trsh_methods: list,
                 available_ess: list = None,
                 is_h: bool = False):
    """
    Troubleshoot issues related to the electronic structure software, such as convergence.

    Args:
        label (str): The species label.
        level_of_theory_dict (dict): The original level of theory dictionary of the problmatic job.
        server (str): The server used for this job.
        job_status (dict): The ESS job status dictionary with standardized error keywords
                           as generated using the `determine_ess_status` function.
        job_type (str): The original job type.
        software (str): The ESS software.
        fine (bool): Whether the job used an ultrafine grid, `True` if it did.
        memory_gb (float): The memory in GB used for the job.
        num_heavy_atoms (int): Number of heavy atoms in a molecule.
        cpu_cores (int): The total number of cpu cores requested for a job.
        ess_trsh_methods (list): The troubleshooting methods tried for this job.
        available_ess (list, optional): Entries are string representations of available ESS.
        is_h (bool): Whether the species is a hydrogen atom (or its isotope). e.g., H, D, T.

    Todo:
        * Change server to one that has the same ESS if running out of disk space.

    Returns:
        output_errors (list): Errors to report.
    Returns:
        ess_trsh_methods (list): The updated troubleshooting methods tried for this job.
    Returns:
        remove_checkfile (bool): Whether to remove the checkfile from the job, `True` to remove.
    Returns:
        level_of_theory_dict (dict): The new level of theory dictionary to use.
    Returns:
        software (str, optional): The new ESS software to use.
    Returns:
        job_type (str): The new job type to use.
    Returns:
        fine (bool): whether the new job should use a fine grid, `True` if it should.
    Returns:
        trsh_keyword (str): The troubleshooting keyword to use.
    Returns:
        memory (float): The new memory in GB to use for the job.
    Returns:
        shift (str): The shift to use (only in Molpro).
    Returns:
        cpus (int): The total number of cpu cores requested for a job.
    Returns:
        couldnt_trsh (bool): Whether a troubleshooting solution was found. `True` if it was not found.
    """
    output_errors = list()
    remove_checkfile, couldnt_trsh = False, False
    trsh_keyword, shift = '', ''
    memory = memory_gb

    if 'memory' not in servers[server]:
        servers[server]['memory'] = 64
        logger.warning(f'A "memory" key (relating to the *maximum* physical node memory) was not defined '
                       f'for server {server}. Setting it to 64 GB (as a guess). This will affect job troubleshooting '
                       f'methods which attempt to increase the job memory. This value should be specified in the '
                       f'servers dictionary in settings.py')

    if 'DiskSpace' in job_status['keywords']:
        output_errors.append(f'Error: Could not troubleshoot {job_type} for {label}! '
                             f'The job ran out of disc space on {server}; ')
        logger.error(f'Could not troubleshoot {job_type} for {label}! The job ran out of disc space on {server}')
        couldnt_trsh = True
    elif 'BasisSet' in job_status['keywords']\
            and ('Unrecognized basis set' in job_status['error']
                 or 'is not appropriate for the this chemistry' in job_status['error']):
        output_errors.append(f'Error: Could not recognize basis set {job_status["error"].split()[-1]} in {software}; ')
        couldnt_trsh = True

    elif software == 'gaussian':
        if 'CheckFile' in job_status['keywords'] and 'checkfie=None' not in ess_trsh_methods:
            # The checkfile doesn't match the new basis set, remove it and rerun the job.
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} that failed with '
                        '"Basis set data is not on the checkpoint file" by removing the checkfile.')
            ess_trsh_methods.append('checkfie=None')
            remove_checkfile = True
        elif 'InternalCoordinateError' in job_status['keywords'] \
                and 'cartesian' not in ess_trsh_methods and job_type == 'opt':
            # try both cartesian and nosymm
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using opt=cartesian with nosyym')
            ess_trsh_methods.append('cartesian')
            trsh_keyword = 'opt=(cartesian,nosymm)'
        elif 'Unconverged' in job_status['keywords'] and 'fine' not in ess_trsh_methods and not fine:
            # try a fine grid for SCF and integral
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using a fine grid')
            ess_trsh_methods.append('fine')
            fine = True
        elif 'SCF' in job_status['keywords'] and 'scf=(qc,nosymm)' not in ess_trsh_methods:
            # try both qc and nosymm
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using scf=(qc,nosymm)')
            ess_trsh_methods.append('scf=(qc,nosymm)')
            trsh_keyword = 'scf=(qc,nosymm)'
        elif 'SCF' in job_status['keywords'] and 'scf=(NDump=30)' not in ess_trsh_methods:
            # Allows dynamic dumping for up to N SCF iterations (slower conversion)
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using scf=(NDump=30)')
            ess_trsh_methods.append('scf=(NDump=30)')
            trsh_keyword = 'scf=(NDump=30)'
        elif 'SCF' in job_status['keywords'] and 'scf=NoDIIS' not in ess_trsh_methods:
            # Switching off Pulay's Direct Inversion
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using scf=NoDIIS')
            ess_trsh_methods.append('scf=NoDIIS')
            trsh_keyword = 'scf=NoDIIS'
        elif 'SCF' in job_status['keywords'] and 'scf=nosymm' not in ess_trsh_methods:
            # try running w/o considering symmetry
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using scf=nosymm')
            ess_trsh_methods.append('scf=nosymm')
            trsh_keyword = 'scf=nosymm'
        elif 'int=(Acc2E=14)' not in ess_trsh_methods:  # does not work in g03
            # Change integral accuracy (skip everything up to 1E-14 instead of 1E-12)
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using int=(Acc2E=14)')
            ess_trsh_methods.append('int=(Acc2E=14)')
            trsh_keyword = 'int=(Acc2E=14)'
        # suggest spwaning a cbs-qb3 job if there are not many heavy atoms
        elif 'cbs-qb3' not in ess_trsh_methods and level_of_theory_dict['method'] != 'cbs-qb3' \
                and 'scan' not in job_type and num_heavy_atoms <= 10:
            # try running CBS-QB3, which is relatively robust.
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using CBS-QB3')
            ess_trsh_methods.append('cbs-qb3')
            level_of_theory_dict['method'] = 'cbs-qb3'
            job_type = 'composite'
        elif 'Memory' in job_status['keywords'] and 'memory' not in ess_trsh_methods:
            # Increase memory allocation
            max_mem = servers[server].get('memory', 128)  # Node memory in GB, defaults to 128 if not specified
            memory = min(memory_gb * 2, max_mem * 0.9)
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using more memory: {memory} GB '
                        f'instead of {memory_gb} GB')
            ess_trsh_methods.append('memory')
        elif level_of_theory_dict['method'] != 'cbs-qb3' and 'scf=(qc,nosymm) & CBS-QB3' not in ess_trsh_methods:
            # try both qc and nosymm with CBS-QB3
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using scf=(qc,nosymm) with CBS-QB3')
            ess_trsh_methods.append('scf=(qc,nosymm) & CBS-QB3')
            level_of_theory_dict['method'] = 'cbs-qb3'
            trsh_keyword = 'scf=(qc,nosymm)'
        elif 'qchem' not in ess_trsh_methods and job_type != 'composite' and \
                (available_ess is None or 'qchem' in [ess.lower() for ess in available_ess]):
            # Try QChem
            logger.info(f'Troubleshooting {job_type} job using qchem instead of {software} for {label}')
            ess_trsh_methods.append('qchem')
            software = 'qchem'
        elif 'molpro' not in ess_trsh_methods and job_type not in ['composite', 'scan'] \
                and (available_ess is None or 'molpro' in [ess.lower() for ess in available_ess]):
            # Try molpro
            logger.info(f'Troubleshooting {job_type} job using molpro instead of {software} for {label}')
            ess_trsh_methods.append('molpro')
            software = 'molpro'
        else:
            couldnt_trsh = True

    elif software == 'qchem':
        if 'MaxOptCycles' in job_status['keywords'] and 'max_cycles' not in ess_trsh_methods:
            # this is a common error, increase max cycles and continue running from last geometry
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using max_cycles')
            ess_trsh_methods.append('max_cycles')
            trsh_keyword = '\n   GEOM_OPT_MAX_CYCLES 250'  # default is 50
        elif 'SCF' in job_status['keywords'] and 'DIIS_GDM' not in ess_trsh_methods:
            # change the SCF algorithm and increase max SCF cycles
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using the DIIS_GDM SCF algorithm')
            ess_trsh_methods.append('DIIS_GDM')
            trsh_keyword = '\n   SCF_ALGORITHM DIIS_GDM\n   MAX_SCF_CYCLES 1000'  # default is 50
        elif 'SYM_IGNORE' not in ess_trsh_methods:  # symmetry - look in manual, no symm if fails
            # change the SCF algorithm and increase max SCF cycles
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using SYM_IGNORE as well as the '
                        f'DIIS_GDM SCF algorithm')
            ess_trsh_methods.append('SYM_IGNORE')
            trsh_keyword = '\n   SCF_ALGORITHM DIIS_GDM\n   MAX_SCF_CYCLES 250\n   SYM_IGNORE     True'
        elif 'wB97X-D3/def2-TZVP' not in ess_trsh_methods:
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using wB97X-D3/def2-TZVP')
            ess_trsh_methods.append('wB97X-D3/def2-TZVP')
            # try converging with wB97X-D3/def2-TZVP
            level_of_theory_dict = {'method': 'wb97x-d3', 'basis': 'def2-tzvp'}
        elif 'b3lyp/6-311++g(d,p)' not in ess_trsh_methods:
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using b3lyp/6-311++g(d,p)')
            ess_trsh_methods.append('b3lyp/6-311++g(d,p)')
            # try converging with B3LYP
            level_of_theory_dict = {'method': 'b3lyp', 'basis': '6-311++g(d,p)'}
        elif 'gaussian' not in ess_trsh_methods \
                and (available_ess is None or 'gaussian' in [ess.lower() for ess in available_ess]):
            # Try Gaussian
            logger.info(f'Troubleshooting {job_type} job using gaussian instead of {software} for {label}')
            ess_trsh_methods.append('gaussian')
            software = 'gaussian'
        elif 'molpro' not in ess_trsh_methods and job_type != 'scan' \
                and (available_ess is None or 'molpro' in [ess.lower() for ess in available_ess]):
            # Try molpro
            logger.info(f'Troubleshooting {job_type} job using molpro instead of {software} for {label}')
            ess_trsh_methods.append('molpro')
            software = 'molpro'
        else:
            couldnt_trsh = True

    elif 'orca' in software:
        if 'Memory' in job_status['keywords']:
            # Increase memory allocation.
            # job_status will be for example
            # `Error  (ORCA_SCF): Not enough memory available! Please increase MaxCore to more than: 289 MB`.
            if 'memory' not in ess_trsh_methods:
                ess_trsh_methods.append('memory')
            estimated_mem_per_core = float(job_status['error'].split()[-2])  # parse Orca's memory requirement in MB
            estimated_mem_per_core = int(np.ceil(estimated_mem_per_core / 100.0)) * 100  # round up to the next hundred
            if 'max_total_job_memory' in job_status['keywords']:
                per_cpu_core_memory = np.ceil(memory_gb / cpu_cores * 1024)
                logger.info(f'The crashed Orca job {label} was ran with {cpu_cores} cpu cores and '
                            f'{per_cpu_core_memory} MB memory per cpu core. It requires at least '
                            f'{estimated_mem_per_core} MB per cpu core. Since the job had already requested the '
                            f'maximum amount of available total node memory, ARC will attempt to reduce the number '
                            f'of cpu cores to increase memory per cpu core.')
                if 'cpu' not in ess_trsh_methods:
                    ess_trsh_methods.append('cpu')
                cpu_cores = math.floor(cpu_cores * per_cpu_core_memory / estimated_mem_per_core) - 2  # be conservative
                if cpu_cores > 1:
                    logger.info(f'Troubleshooting job {label} using {cpu_cores} cpu cores.')
                elif cpu_cores == 1:  # last resort
                    logger.info(f'Troubleshooting job {label} using only {cpu_cores} cpu core. Notice that the '
                                f'required job time may be unrealistically long or exceed limits on servers.')
                else:
                    logger.info(f'Not enough computational resource to accomplish job {label}. Please consider cheaper '
                                f'methods or allocate more resources if possible.')
                    couldnt_trsh = True
            if not couldnt_trsh:
                memory = estimated_mem_per_core * cpu_cores  # total memory for all cpu cores
                memory = np.ceil(memory / 1024 + 5)  # convert MB to GB, add 5 extra GB (be conservative)
                logger.info(f'Troubleshooting {job_type} job in {software} for {label} using {memory} GB total memory '
                            f'and {cpu_cores} cpu cores.')
        elif 'cpu' in job_status['keywords']:
            # Reduce cpu allocation.
            # job_status will be for example
            # Error (ORCA_MDCI): Number of processes (16) in parallel calculation exceeds number of pairs (10)
            cpu_cores = int(job_status['error'].split()[-1].strip('.'))  # max_cpu_cores_allowed
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using {cpu_cores} cpu cores '
                        f'(reduced).')
            if 'cpu' not in ess_trsh_methods:
                ess_trsh_methods.append('cpu')
        elif 'dlpno' in level_of_theory_dict['method'] and is_h:
            logger.error(f'DLPNO method is not supported for H atom (or its isotope D or T) in Orca.')
            couldnt_trsh = True
        else:
            couldnt_trsh = True

    elif 'molpro' in software:
        if 'Memory' in job_status['keywords']:
            # Increase memory allocation.
            # molpro gives something like `'errored: additional memory (mW) required: 996.31'`.
            # job_status standardizes the format to be:  `'Additional memory required: {0} MW'`
            # The number is the ADDITIONAL memory required in GB
            ess_trsh_methods.append('memory')
            add_mem = float(job_status['error'].split()[-2])  # parse Molpro's requirement in MW
            add_mem = int(np.ceil(add_mem / 100.0)) * 100  # round up to the next hundred
            memory = memory_gb + add_mem / 128. + 5  # convert MW to GB, add 5 extra GB (be conservative)
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using memory: {memory:.2f} GB '
                        f'instead of {memory_gb} GB')
        elif 'shift' not in ess_trsh_methods:
            # Try adding a level shift for alpha- and beta-spin orbitals
            # Applying large negative level shifts like {rhf; shift,-1.0,-0.5}
            # will often stabilize convergence at the expense of making it somewhat slower.
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using shift')
            ess_trsh_methods.append('shift')
            shift = 'shift,-1.0,-0.5;'
        elif 'vdz' not in ess_trsh_methods:
            # degrade the basis set
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using vdz')
            ess_trsh_methods.append('vdz')
            trsh_keyword = 'vdz'
        elif 'vdz & shift' not in ess_trsh_methods:
            # try adding a level shift for alpha- and beta-spin orbitals
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using vdz')
            ess_trsh_methods.append('vdz & shift')
            shift = 'shift,-1.0,-0.5;'
            trsh_keyword = 'vdz'
        elif 'memory' not in ess_trsh_methods:
            # Increase memory allocation, also run with a shift
            ess_trsh_methods.append('memory')
            memory = servers[server]['memory']  # set memory to the value of an entire node (in GB)
            logger.info(f'Troubleshooting {job_type} job in {software} for {label} using memory: {memory:.2f} GB '
                        f'instead of {memory_gb} GB')
            shift = 'shift,-1.0,-0.5;'
        elif 'gaussian' not in ess_trsh_methods\
                and (available_ess is None or 'gaussian' in [ess.lower() for ess in available_ess]):
            # Try Gaussian
            logger.info(f'Troubleshooting {job_type} job using gaussian instead of {software} for {label}')
            ess_trsh_methods.append('gaussian')
            software = 'gaussian'
        elif 'qchem' not in ess_trsh_methods\
                and (available_ess is None or 'qchem' in [ess.lower() for ess in available_ess]):
            # Try QChem
            logger.info(f'Troubleshooting {job_type} job using qchem instead of {software} for {label}')
            ess_trsh_methods.append('qchem')
            software = 'qchem'
        else:
            couldnt_trsh = True

    elif 'terachem' in software:
        """
        scf diis+a
        maxit 50
        
        solve in freq:
        Maximum gradient component at reference geometry: 2.19e-02
        Maximum component of gradient is too large
        Optimize the geometry and try again
        """
        couldnt_trsh = True

    else:
        logger.error(f'Troubleshooting methods are not implemented for {software}')
        couldnt_trsh = True

    if couldnt_trsh:
        logger.error(f'Could not troubleshoot geometry optimization for {label}! '
                     f'Tried troubleshooting with the following methods: {ess_trsh_methods}')
        output_errors.append(f'Error: Could not troubleshoot {job_type} for {label}! '
                             f'Tried troubleshooting with the following methods: {ess_trsh_methods}; ')
    return output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, \
        trsh_keyword, memory, shift, cpu_cores, couldnt_trsh


def trsh_conformer_isomorphism(software: str,
                               ess_trsh_methods: list = None,
                               ) -> str:
    """
    Troubleshoot conformer optimization for a species that failed isomorphic test in
    `determine_most_stable_conformer` by specifying a "good" level of theory.

    Args:
        software (str): The ESS used.
        ess_trsh_methods (list, optional): The troubleshooting methods tried for this job.

    Raises:
        TrshError: If the requested ``ess_trsh_methods`` is not supported.

    Returns:
        level_of_theory (str): Tte level of theory to troubleshoot at.
    """
    ess_trsh_methods = ess_trsh_methods if ess_trsh_methods is not None else list()
    if software == 'gaussian':
        conformer_trsh_methods = ['wb97xd/def2TZVP', 'apfd/def2TZVP']
    elif software == 'qchem':
        conformer_trsh_methods = ['wb97x-d3/def2-TZVP']
    elif software == 'orca':
        conformer_trsh_methods = ['wB97X-D3/def2-TZVP']
    elif software == 'terachem':
        conformer_trsh_methods = ['wb97xd3/def2-TZVP']
    else:
        raise TrshError(f'The troubleshoot_conformer_isomorphism() method is not implemented for {software}.')

    level_of_theory = None
    for method in conformer_trsh_methods:
        if 'conformer ' + method in ess_trsh_methods:
            continue
        ess_trsh_methods.append('conformer ' + method)
        level_of_theory = method
        break
    return level_of_theory


def trsh_job_on_server(server: str,
                       job_name: str,
                       job_id: str,
                       job_server_status: str,
                       remote_path: str,
                       server_nodes: list = None):
    """
    Troubleshoot server errors.

    Args:
        server (str): The server name.
        job_name (str): The job's name (e.g., 'opt_a103').
        job_id (str): The job's ID on the server.
        job_server_status (str): The job server status (either 'initializing', 'running', 'errored', or 'done').
        remote_path (str): The remote path to the job folder.
        server_nodes (list, optional): The nodes already tried on this server for this jobs.

    Returns:
        str: The new node on the server (or None).
    Returns:
        bool: Whether to re-run the job, `True` to rerun.
    """
    server_nodes = server_nodes if server_nodes is not None else list()
    if job_server_status != 'done':
        logger.error(f'Job {job_name} has server status "{job_server_status}" on {server}.')

    # delete current server run
    command = delete_command[servers[server]['cluster_soft']] + ' ' + str(job_id)
    if server == 'local':
        execute_command(command)
        return None, True
    else:
        ssh = SSHClient(server)
        ssh.send_command_to_server(command)

    if servers[server]['cluster_soft'].lower() == 'oge':
        logger.error('Troubleshooting by changing node.')
        ssh = SSHClient(server)
        # find available nodes
        stdout = ssh.send_command_to_server(command=list_available_nodes_command[servers[server]['cluster_soft']])[0]
        for line in stdout:
            node = line.split()[0].split('.')[0].split('node')[1]
            if servers[server]['cluster_soft'] == 'OGE' and '0/0/8' in line and node not in server_nodes:
                server_nodes.append(node)
                break
        else:
            logger.error(f'Could not find an available node on the server {server}')
            # TODO: continue troubleshooting; if all else fails, put the job to sleep,
            #       and try again searching for a node
            return None, False

        # modify the submit file
        content = ssh.read_remote_file(remote_path=remote_path,
                                       filename=submit_filename[servers[server]['cluster_soft']])
        for i, line in enumerate(content):
            if '#$ -l h=node' in line:
                content[i] = '#$ -l h=node{0}.cluster'.format(node)
                break
        else:
            content.insert(7, '#$ -l h=node{0}.cluster'.format(node))
        content = ''.join(content)  # convert list into a single string, not to upset paramiko
        # resubmit
        ssh.upload_file(remote_file_path=os.path.join(remote_path,
                        submit_filename[servers[server]['cluster_soft']]), file_string=content)
        return node, True

    elif servers[server]['cluster_soft'].lower() == 'slurm':
        # TODO: change node on Slurm
        return None, True

    return None, False


def scan_quality_check(label: str,
                       pivots: list,
                       energies: list,
                       scan_res: float = rotor_scan_resolution,
                       used_methods: list = None,
                       species: ARCSpecies = None,
                       preserve_params: list = None,
                       trajectory: list = None,
                       original_xyz: dict = None,
                       ):
    """
    Checks the scan's quality:
    - Whether the initial and final points are consistent
    - whether it is relatively "smooth"
    - whether atom distances to preserve aren't violated
    - whether the optimized geometry indeed represents the minimum energy conformer (for a non-TS)
    - whether the barrier height is reasonable
    Recommends whether or not to use this rotor using the 'successful_rotors' and 'unsuccessful_rotors' attributes.

    Args:
        label (str): The species label.
        pivots (list): The rotor pivots.
        energies (list): The scan energies in kJ/mol.
        scan_res (float, optional): The scan resolution in degrees.
        used_methods (list, optional): Troubleshooting methods already tried out.
        species (ARCSpecies, optional): The ARCSpecies this scan is related to.
        preserve_params (list, optional): Entries are length 2 lists of atom indices (1-indexed) between which the
                                          distance as well as a torsion dihedral angle with these atoms as its pivots
                                          must be preserved throughout the scan to a tolerance.
        trajectory (list, optional): Entries are Cartesian coordinates along the scan trajectory.
        original_xyz (dict, optional): The optimized coordinated for the species.

    Returns:
        invalidate (bool): Whether to invalidate this rotor, ``True`` to invalidate.
    Returns:
        invalidation_reason (str): Reason for invalidating this rotor.
    Returns:
        message (str): Error or warning message.
    Returns:
        actions (list): Troubleshooting methods to apply, including conformational changes.

    Todo:
        - adjust to ND
    """
    message, invalidation_reason = '', ''
    invalidate = False
    actions = list()
    used_methods = used_methods or list()
    energies = np.array(energies, np.float64)

    # 1. Check rotor scan curve
    # 1.1. Check consistency between initial and final points
    if abs(energies[-1] - energies[0]) > inconsistency_az:
        # initial and final points differ by more than `inconsistency_az` kJ/mol.
        # seems like this rotor broke the conformer. Invalidate
        invalidate = True
        invalidation_reason = f'initial and final points are inconsistent by more than {inconsistency_az:.2f} kJ/mol'
        message = f'Rotor scan of {label} between pivots {pivots} is inconsistent by more ' \
                  f'than {inconsistency_az:.2f} kJ/mol between initial and final positions. ' \
                  f'Invalidating rotor.\nenergies[0] = {energies[0]}, energies[-1] = {energies[-1]}'
        logger.error(message)
        actions = ['inc_res', 'freeze']
        return invalidate, invalidation_reason, message, actions

    # 1.2. Check consistency between consecutive points
    for j in range(len(energies) - 1):
        if abs(energies[j] - energies[j + 1]) > inconsistency_ab * np.max(energies):
            # Two consecutive points on the scan differ by more than `inconsistency_ab` kJ/mol.
            # This is a serious inconsistency. Invalidate
            invalidate = True
            invalidation_reason = f'Two consecutive points are inconsistent by more than ' \
                                  f'{inconsistency_ab * max(energies):.2f} kJ/mol'
            message = f'Rotor scan of {label} between pivots {pivots} is inconsistent ' \
                      f'by more than {inconsistency_ab * max(energies):.2f} kJ/mol between two consecutive ' \
                      f'points. Invalidating rotor.'
            logger.error(message)
            if ['inc_res'] not in used_methods:
                actions = ['inc_res']
            elif ['inc_res', 'freeze'] not in used_methods:
                actions = ['inc_res', 'freeze']
            return invalidate, invalidation_reason, message, actions

    # 2. Check TS atom distance constraints are preserved
    if preserve_params is not None:
        threshold = preserve_param_in_scan_ratio
        success = True
        pivots = list()
        for atoms in preserve_params:
            for i, xyz in enumerate(trajectory):
                if i != 0:
                    # check that the distance between this atom pair is preserved relative to the previous entry
                    # in the trajectory, as well as relative to the final_xyz.
                    current_distance = calculate_distance(coords=xyz, atoms=atoms, index=1)
                    previous_distance = calculate_distance(coords=trajectory[i-1], atoms=atoms, index=1)
                    original_distance = calculate_distance(coords=original_xyz, atoms=atoms, index=1)
                    if 1.0 + threshold < current_distance / previous_distance < 1.0 / (1.0 + threshold) \
                            or 1.0 + threshold < current_distance / original_distance < 1.0 / (1.0 + threshold):
                        success = False
                        pivots.append(atoms)
                        message = f'The rotor breaks the TS around pivots {pivots}: In trajectory {i}, the distance ' \
                                  f'between the pivots is {current_distance} Angstroms, a ' \
                                  f'{current_distance / previous_distance} change relative to the previous ' \
                                  f'frame, a {current_distance / original_distance} change relative to the original ' \
                                  f'geometry (greater than {1.0 + threshold} or smaller than {1.0 / (1.0 + threshold)}).'
                        break

                    if species.mol is not None:
                        scan = [determine_smallest_atom_index_in_scan(atom1=species.mol.atoms.index(atoms[0]),
                                                                      atom2=species.mol.atoms.index(atoms[1]),
                                                                      mol=species.mol)]
                        scan.extend(atoms)
                        scan.append(determine_smallest_atom_index_in_scan(atom1=species.mol.atoms.index(atoms[1]),
                                                                          atom2=species.mol.atoms.index(atoms[0]),
                                                                          mol=species.mol))
                        # check that a dihedral angle with this atom pair as its pivots is preserved relative to the
                        # previous entry in the trajectory, as well as relative to the final_xyz.
                        current_dihedral = calculate_dihedral_angle(coords=xyz, torsion=scan, index=1)
                        previous_dihedral = calculate_dihedral_angle(coords=trajectory[i - 1], torsion=scan, index=1)
                        original_dihedral = calculate_dihedral_angle(coords=original_xyz, torsion=scan, index=1)
                        if 1.0 + threshold < current_dihedral / previous_dihedral < 1.0 / (1.0 + threshold) \
                                or 1.0 + threshold < current_dihedral / original_dihedral < 1.0 / (1.0 + threshold):
                            success = False
                            pivots.append(atoms)
                            message = f'The rotor breaks the TS around pivots {pivots}: In trajectory {i}, the ' \
                                      f'dihedral angle with these pivots is {current_dihedral} degrees, a ' \
                                      f'{current_dihedral / previous_dihedral} change relative to the previous ' \
                                      f'frame, a {current_dihedral / original_dihedral} change relative to the ' \
                                      f'original geometry (greater than {1.0 + threshold} or smaller than ' \
                                      f'{1.0 / (1.0 + threshold)}).'
                            break

        if species.mol is None:
            logger.warning(f'Cannot check that the dihedral angle of {species.label} is consistent throughout rotor '
                           f'scans without a .mol attribute')
        if not success:
            invalidate = True
            invalidation_reason = message
            logger.info(message)
            actions = list()
            return invalidate, invalidation_reason, message, actions

    # 3. Check conformation:
    if species is None or not species.is_ts:
        # not checking conformation for a TS, since the scan could be a coordinate to/from the saddle
        energy_diff = energies[0] - np.min(energies)
        if energy_diff >= 2 or energy_diff > 0.5 * (max(energies) - min(energies)):
            invalidate = True
            invalidation_reason = f'Another conformer for {label} exists which is {energy_diff:.2f} kJ/mol lower.'
            message = f'Species {label} is not oriented correctly around pivots {pivots}. ' \
                      f'Another conformer exists which is {energy_diff:.2f} kJ/mol lower. ' \
                      f'searching for a better conformation...'
            logger.info(message)
            # Find the rotation dihedral in degrees to the closest minimum:
            min_index = np.argmin(energies)
            deg_increment = float(min_index * scan_res)
            actions = ['change conformer', pivots, deg_increment]
            if actions in used_methods:
                logger.error(f'Not troubleshooting a rotor with the same method: {actions}')
                actions = list()
            return invalidate, invalidation_reason, message, actions

    # 4. Check the barrier height
    if (np.max(energies) - np.min(energies)) > maximum_barrier:
        # The barrier for the internal rotation is higher than `maximum_barrier`
        num_wells = determine_rotor_symmetry(label=label,
                                             pivots=pivots,
                                             rotor_path='',
                                             energies=energies,
                                             return_num_wells=True)[-1]
        if num_wells == 1:
            invalidate = True
            invalidation_reason = f'The rotor scan has a barrier of {np.max(energies) - np.min(energies):.2f} ' \
                                  f'kJ/mol, which is higher than the maximal barrier for rotation ' \
                                  f'({maximum_barrier:.2f} kJ/mol)'
            message = f'Rotor scan of {label} between pivots {pivots} has a barrier ' \
                      f'larger than {maximum_barrier:.2f} kJ/mol. Invalidating rotor.'
            logger.warning(message)
            return invalidate, invalidation_reason, message, actions
        else:
            logger.warning(f'The maximal barrier for rotor {pivots} of {label} is '
                           f'{(np.max(energies) - np.min(energies))} kJ/mol, which is higher than the set threshold '
                           f'of {maximum_barrier} kJ/mol. Since this mode when treated as torsion has {num_wells}, '
                           f'this mode is not invalidated: treating it as a vibrational mode will be less accurate than'
                           f'the a hindered rotor treatment, since the entropy contribution from the population of '
                           f'this species at the higher wells will not be taken into account. NOT invalidating this '
                           f'torsional mode.')

    return invalidate, invalidation_reason, message, actions
