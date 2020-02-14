#!/usr/bin/env python3
# encoding: utf-8

"""
A module for representing a reaction.
"""

from rmgpy.reaction import Reaction
from rmgpy.species import Species

import arc.rmgdb as rmgdb
from arc.common import get_logger
from arc.exceptions import ReactionError, InputError
from arc.settings import default_ts_methods
from arc.species.species import ARCSpecies


logger = get_logger()


class ARCReaction(object):
    """
    A class for representing a chemical reaction.

    Either give reactants and products (just list of labels corresponding to :ref:`ARCSpecies <species>`),
    a reaction label, or an RMG Reaction object.
    If the reactants and products in the RMG Reaction aren't ARCSpecies, they will be created.

    The ARCReaction object stores the labels corresponding to the reactants, products and TS ARCSpecies objects
    as self.reactants, self.products, and self.ts_label, respectively.

    Args:
        label (str, optional): The reaction's label in the format `r1 + r2 <=> p1 + p2`
                               (or unimolecular on either side, as appropriate).
        reactants (list, optional): A list of reactants labels corresponding to an :ref:`ARCSpecies <species>`.
        products (list, optional): A list of products labels corresponding to an :ref:`ARCSpecies <species>`.
        ts_label (str, optional): The :ref:`ARCSpecies <species>` label of the respective TS.
        rmg_reaction (Reaction, optional): An RMG Reaction class.
        ts_methods (list, optional): Methods to try for generating TS guesses. If an ARCSpecies is a TS and ts_methods
                                     is empty (passing an empty list), then xyz (user guess) must be given.
        ts_xyz_guess (list, optional): A list of TS XYZ user guesses, each in a string format.
        multiplicity (int, optional): The reaction surface multiplicity. A trivial guess will be made unless provided.
        charge (int, optional): The reaction surface charge.
        reaction_dict (dict, optional): A dictionary to create this object from (used when restarting ARC).

    Attributes:
        label (str): The reaction's label in the format `r1 + r2 <=> p1 + p2`
                     (or unimolecular on either side, as appropriate).
        family (str): The RMG kinetic family, if applicable.
        family_own_reverse (bool): Whether the RMG family is its own reverse.
        reactants (list): A list of reactants labels corresponding to an :ref:`ARCSpecies <species>`.
        products (list): A list of products labels corresponding to an :ref:`ARCSpecies <species>`.
        r_species (list): A list of reactants :ref:`ARCSpecies <species>` objects.
        p_species (list): A list of products :ref:`ARCSpecies <species>` objects.
        ts_species (ARCSpecies): The :ref:`ARCSpecies <species>` corresponding to the reaction's TS.
        dh_rxn298 (float):  The heat of reaction at 298K.
        kinetics (Arrhenius): The kinetics calculated by ARC.
        rmg_kinetics (Arrhenius): The kinetics generated by RMG, for reality-check.
        rmg_reaction (Reaction): An RMG Reaction class.
        rmg_reactions (list): A list of RMG Reaction objects with RMG rates for comparisons.
        long_kinetic_description (str): A description for the species entry in the thermo library outputted.
        ts_methods (list): Methods to try for generating TS guesses. If an ARCSpecies is a TS and ts_methods
                           is empty (passing an empty list), then xyz (user guess) must be given.
        ts_xyz_guess (list): A list of TS XYZ user guesses, each in a string format.
        multiplicity (int): The reaction surface multiplicity. A trivial guess will be made unless provided.
        charge (int): The reaction surface charge.
        index (int): An auto-generated index associating the ARCReaction object with the
                     corresponding TS :ref:`ARCSpecies <species>` object.
        ts_label (str): The :ref:`ARCSpecies <species>` label of the respective TS.
    """
    def __init__(self, label='', reactants=None, products=None, ts_label=None, rmg_reaction=None,
                 ts_methods=None, ts_xyz_guess=None, multiplicity=None, charge=0, reaction_dict=None):
        self.arrow = ' <=> '
        self.plus = ' + '
        self.r_species = list()
        self.p_species = list()
        self.kinetics = None
        self.rmg_kinetics = None
        self.long_kinetic_description = ''
        self.family = None
        self.family_own_reverse = 0
        self.ts_label = ts_label
        self.dh_rxn298 = None
        self.rmg_reactions = None
        if ts_xyz_guess is not None and not isinstance(ts_xyz_guess, list):
            ts_xyz_guess = [ts_xyz_guess]
        if reaction_dict is not None:
            # Reading from a dictionary
            self.from_dict(reaction_dict=reaction_dict)
        else:
            # Not reading from a dictionary
            self.label = label
            self.index = None
            self.ts_species = None
            self.multiplicity = multiplicity
            self.charge = charge
            if self.multiplicity is not None and not isinstance(self.multiplicity, int):
                raise InputError('Reaction multiplicity must be an integer, got {0} of type {1}.'.format(
                    self.multiplicity, type(self.multiplicity)))
            self.reactants = reactants
            self.products = products
            self.rmg_reaction = rmg_reaction
            if self.rmg_reaction is None and (self.reactants is None or self.products is None) and not self.label:
                raise InputError('Cannot determine reactants and/or products labels for reaction {0}'.format(
                    self.label))
            self.set_label_reactants_products()
            self.ts_methods = ts_methods if ts_methods is not None else default_ts_methods
            self.ts_methods = [tsm.lower() for tsm in self.ts_methods]
            self.ts_xyz_guess = ts_xyz_guess if ts_xyz_guess is not None else list()
        if len(self.reactants) > 3 or len(self.products) > 3:
            raise ReactionError('An ARC Reaction can have up to three reactants / products. got {0} reactants'
                                ' and {1} products for reaction {2}.'.format(len(self.reactants), len(self.products),
                                                                             self.label))

    def as_dict(self):
        """A helper function for dumping this object as a dictionary in a YAML file for restarting ARC"""
        reaction_dict = dict()
        reaction_dict['label'] = self.label
        reaction_dict['index'] = self.index
        reaction_dict['multiplicity'] = self.multiplicity
        reaction_dict['charge'] = self.charge
        reaction_dict['reactants'] = self.reactants
        reaction_dict['products'] = self.products
        reaction_dict['r_species'] = [spc.as_dict() for spc in self.r_species]
        reaction_dict['p_species'] = [spc.as_dict() for spc in self.p_species]
        if self.ts_species is not None:
            reaction_dict['ts_species'] = self.ts_species.as_dict()
        if 'rmg_reaction' in reaction_dict:
            reaction_dict['rmg_reaction'] = self.rmg_reaction_to_str()
        reaction_dict['family'] = self.family
        reaction_dict['family_own_reverse'] = self.family_own_reverse
        reaction_dict['long_kinetic_description'] = self.long_kinetic_description
        reaction_dict['label'] = self.label
        reaction_dict['ts_methods'] = self.ts_methods
        reaction_dict['ts_xyz_guess'] = self.ts_xyz_guess
        reaction_dict['ts_label'] = self.ts_label
        return reaction_dict

    def from_dict(self, reaction_dict):
        """
        A helper function for loading this object from a dictionary in a YAML file for restarting ARC
        """
        self.index = reaction_dict['index'] if 'index' in reaction_dict else None
        self.label = reaction_dict['label'] if 'label' in reaction_dict else ''
        self.multiplicity = reaction_dict['multiplicity'] if 'multiplicity' in reaction_dict else None
        self.charge = reaction_dict['charge'] if 'charge' in reaction_dict else 0
        self.reactants = reaction_dict['reactants'] if 'reactants' in reaction_dict else None
        self.products = reaction_dict['products'] if 'products' in reaction_dict else None
        self.family = reaction_dict['family'] if 'family' in reaction_dict else None
        self.family_own_reverse = reaction_dict['family_own_reverse'] if 'family_own_reverse' in reaction_dict else 0
        if 'rmg_reaction' in reaction_dict:
            self.rmg_reaction_from_str(reaction_string=reaction_dict['rmg_reaction'])
        else:
            self.rmg_reaction = None
        self.set_label_reactants_products()
        if self.rmg_reaction is None and (self.reactants is None or self.products is None):
            raise InputError('Cannot determine reactants and/or products labels for reaction {0}'.format(
                self.label))
        if self.reactants is None or self.products is None:
            if not all([spc.label for spc in self.rmg_reaction.reactants + self.rmg_reaction.products]):
                raise InputError('All species in a reaction must be labeled (and the labels must correspond'
                                 ' to respective Species in ARC). If an RMG Reaction object was passes, make'
                                 ' sure that all species in the reactants and products are correctly labeled.'
                                 ' Problematic reaction: {0}'.format(self.label))
            self.reactants = [spc.label for spc in self.rmg_reaction.reactants]
            self.products = [spc.label for spc in self.rmg_reaction.products]
        self.set_label_reactants_products()
        if self.ts_label is None:
            self.ts_label = reaction_dict['ts_label'] if 'ts_label' in reaction_dict else None
        self.r_species = [r.from_dict() for r in reaction_dict['r_species']] if 'r_species' in reaction_dict else list()
        self.p_species = [p.from_dict() for p in reaction_dict['p_species']] if 'p_species' in reaction_dict else list()
        self.ts_species = reaction_dict['ts_species'].from_dict() if 'ts_species' in reaction_dict else None

        self.long_kinetic_description = reaction_dict['long_kinetic_description']\
            if 'long_kinetic_description' in reaction_dict else ''
        self.ts_methods = reaction_dict['ts_methods'] if 'ts_methods' in reaction_dict else default_ts_methods
        self.ts_methods = [tsm.lower() for tsm in self.ts_methods]
        self.ts_xyz_guess = reaction_dict['ts_xyz_guess'] if 'ts_xyz_guess' in reaction_dict else list()

    def set_label_reactants_products(self):
        """A helper function for settings the label, reactants, and products attributes for a Reaction"""
        # first make sure that reactants and products labels are defines (most often used)
        if self.reactants is None or self.products is None:
            if self.label:
                if self.arrow not in self.label:
                    raise ReactionError('A reaction label must contain an arrow ("{0}")'.format(self.arrow))
                reactants, products = self.label.split(self.arrow)
                if self.plus in reactants:
                    self.reactants = reactants.split(self.plus)
                else:
                    self.reactants = [reactants]
                if self.plus in products:
                    self.products = products.split(self.plus)
                else:
                    self.products = [products]
            elif self.rmg_reaction is not None:
                self.reactants = [r.label for r in self.rmg_reaction.reactants]
                self.products = [p.label for p in self.rmg_reaction.products]
        if not self.label:
            if self.reactants is not None and self.products is not None:
                self.label = self.arrow.join([self.plus.join(r for r in self.reactants),
                                              self.plus.join(p for p in self.products)])
            elif self.r_species is not None and self.p_species is not None:
                self.label = self.arrow.join([self.plus.join(r.label for r in self.r_species),
                                              self.plus.join(p.label for p in self.p_species)])
            elif self.rmg_reaction is not None:
                # this will probably never be executed, but OK to keep
                self.label = self.arrow.join([self.plus.join(r.label for r in self.rmg_reaction.reactants),
                                              self.plus.join(p.label for p in self.rmg_reaction.products)])
        if self.rmg_reaction is None:
            self.rmg_reaction_from_arc_species()
        elif not self.label and (self.reactants is None or self.products is None):
            raise ReactionError('Either a label or reactants and products lists must be specified')

    def rmg_reaction_to_str(self):
        """A helper function for dumping the RMG Reaction object as a string for the YAML restart dictionary"""
        return self.arrow.join([self.plus.join(r.molecule[0].to_smiles() for r in self.rmg_reaction.reactants),
                                self.plus.join(p.molecule[0].to_smiles() for p in self.rmg_reaction.products)])

    def rmg_reaction_from_str(self, reaction_string):
        """A helper function for regenerating the RMG Reaction object from a string representation"""
        reactants, products = reaction_string.split(self.arrow)
        reactants = [Species().from_smiles(smiles) for smiles in reactants.split(self.plus)]
        products = [Species().from_smiles(smiles) for smiles in products.split(self.plus)]
        self.rmg_reaction = Reaction(reactants=reactants, products=products)

    def rmg_reaction_from_arc_species(self):
        """
        A helper function for generating the RMG Reaction object from ARCSpecies
        Used for determining the family
        """
        if self.rmg_reaction is None and len(self.r_species) and len(self.p_species) and \
                all([arc_spc.mol is not None for arc_spc in self.r_species + self.p_species]):
            reactants = [Species(molecule=[r.mol]) for r in self.r_species]
            for i, reac in enumerate(self.r_species):
                reactants[i].label = reac.label
            products = [Species(molecule=[p.mol]) for p in self.p_species]
            for i, prod in enumerate(self.p_species):
                products[i].label = prod.label
            self.rmg_reaction = Reaction(reactants=reactants, products=products)

    def arc_species_from_rmg_reaction(self):
        """
        A helper function for generating the ARC Species (.r_species and .p_species) from the RMG Reaction object
        """
        if self.rmg_reaction is not None and not len(self.r_species) and not len(self.p_species):
            self.r_species = [ARCSpecies(label=spc.label, mol=spc.molecule[0]) for spc in self.rmg_reaction.reactants]
            self.p_species = [ARCSpecies(label=spc.label, mol=spc.molecule[0]) for spc in self.rmg_reaction.products]

    def determine_rxn_multiplicity(self):
        """A helper function for determining the surface multiplicity"""
        if self.multiplicity is None:
            ordered_r_mult_list, ordered_p_mult_list = list(), list()
            if len(self.r_species):
                if len(self.r_species) == 1:
                    self.multiplicity = self.r_species[0].multiplicity
                elif len(self.r_species) == 2:
                    ordered_r_mult_list = sorted([self.r_species[0].multiplicity,
                                                  self.r_species[1].multiplicity])
                elif len(self.r_species) == 3:
                    ordered_r_mult_list = sorted([self.r_species[0].multiplicity,
                                                  self.r_species[1].multiplicity,
                                                  self.r_species[2].multiplicity])
                if len(self.p_species) == 1:
                    self.multiplicity = self.p_species[0].multiplicity
                elif len(self.p_species) == 2:
                    ordered_p_mult_list = sorted([self.p_species[0].multiplicity,
                                                  self.p_species[1].multiplicity])
                elif len(self.p_species) == 3:
                    ordered_p_mult_list = sorted([self.p_species[0].multiplicity,
                                                  self.p_species[1].multiplicity,
                                                  self.p_species[2].multiplicity])
            elif self.rmg_reaction is not None:
                if len(self.rmg_reaction.reactants) == 1:
                    self.multiplicity = self.rmg_reaction.reactants[0].molecule[0].multiplicity
                elif len(self.rmg_reaction.reactants) == 2:
                    ordered_r_mult_list = sorted([self.rmg_reaction.reactants[0].molecule[0].multiplicity,
                                                  self.rmg_reaction.reactants[1].molecule[0].multiplicity])
                elif len(self.rmg_reaction.reactants) == 3:
                    ordered_r_mult_list = sorted([self.rmg_reaction.reactants[0].molecule[0].multiplicity,
                                                  self.rmg_reaction.reactants[1].molecule[0].multiplicity,
                                                  self.rmg_reaction.reactants[2].molecule[0].multiplicity])
                if len(self.rmg_reaction.products) == 1:
                    self.multiplicity = self.rmg_reaction.products[0].molecule[0].multiplicity
                elif len(self.rmg_reaction.products) == 2:
                    ordered_p_mult_list = sorted([self.rmg_reaction.products[0].molecule[0].multiplicity,
                                                  self.rmg_reaction.products[1].molecule[0].multiplicity])
                elif len(self.rmg_reaction.products) == 3:
                    ordered_p_mult_list = sorted([self.rmg_reaction.products[0].molecule[0].multiplicity,
                                                  self.rmg_reaction.products[1].molecule[0].multiplicity,
                                                  self.rmg_reaction.products[2].molecule[0].multiplicity])
            if self.multiplicity is None:
                if ordered_r_mult_list == [1, 1]:
                    self.multiplicity = 1  # S + S = D
                elif ordered_r_mult_list == [1, 2]:
                    self.multiplicity = 2  # S + D = D
                elif ordered_r_mult_list == [2, 2]:
                    # D + D = S or T
                    if ordered_p_mult_list in [[1, 1], [1, 1, 1]]:
                        self.multiplicity = 1
                    elif ordered_p_mult_list in [[1, 3], [1, 1, 3]]:
                        self.multiplicity = 3
                    else:
                        self.multiplicity = 1
                        logger.warning('ASSUMING a multiplicity of 1 (singlet) for reaction {0}'.format(self.label))
                elif ordered_r_mult_list == [1, 3]:
                    self.multiplicity = 3  # S + T = T
                elif ordered_r_mult_list == [2, 3]:
                    # D + T = D or Q
                    if ordered_p_mult_list in [[1, 2], [1, 1, 2]]:
                        self.multiplicity = 2
                    elif ordered_p_mult_list in [[1, 4], [1, 1, 4]]:
                        self.multiplicity = 4
                    else:
                        self.multiplicity = 2
                        logger.warning('ASSUMING a multiplicity of 2 (doublet) for reaction {0}'.format(self.label))
                elif ordered_r_mult_list == [3, 3]:
                    # T + T = S or T or quintet
                    if ordered_p_mult_list in [[1, 1], [1, 1, 1]]:
                        self.multiplicity = 1
                    elif ordered_p_mult_list in [[1, 3], [1, 1, 3]]:
                        self.multiplicity = 3
                    elif ordered_p_mult_list in [[1, 5], [1, 1, 5]]:
                        self.multiplicity = 5
                    else:
                        self.multiplicity = 3
                        logger.warning('ASSUMING a multiplicity of 3 (triplet) for reaction {0}'.format(self.label))
                elif ordered_r_mult_list == [1, 1, 1]:
                    self.multiplicity = 1  # S + S + S = S
                elif ordered_r_mult_list == [1, 1, 2]:
                    self.multiplicity = 2  # S + S + D = D
                elif ordered_r_mult_list == [1, 1, 3]:
                    self.multiplicity = 3  # S + S + T = T
                elif ordered_r_mult_list == [1, 2, 2]:
                    # S + D + D = S or T
                    if ordered_p_mult_list in [[1, 1], [1, 1, 1]]:
                        self.multiplicity = 1
                    elif ordered_p_mult_list in [[1, 3], [1, 1, 3]]:
                        self.multiplicity = 3
                    else:
                        self.multiplicity = 1
                        logger.warning('ASSUMING a multiplicity of 1 (singlet) for reaction {0}'.format(self.label))
                elif ordered_r_mult_list == [2, 2, 2]:
                    # D + D + D = D or Q
                    if ordered_p_mult_list in [[1, 2], [1, 1, 2]]:
                        self.multiplicity = 2
                    elif ordered_p_mult_list in [[1, 4], [1, 1, 4]]:
                        self.multiplicity = 4
                    else:
                        self.multiplicity = 2
                        logger.warning('ASSUMING a multiplicity of 2 (doublet) for reaction {0}'.format(self.label))
                elif ordered_r_mult_list == [1, 2, 3]:
                    # S + D + T = D or Q
                    if ordered_p_mult_list in [[1, 2], [1, 1, 2]]:
                        self.multiplicity = 2
                    elif ordered_p_mult_list in [[1, 4], [1, 1, 4]]:
                        self.multiplicity = 4
                    self.multiplicity = 2
                    logger.warning('ASSUMING a multiplicity of 2 (doublet) for reaction {0}'.format(self.label))
                else:
                    raise ReactionError('Could not determine multiplicity for reaction {0}, please input it.'.format(
                        self.multiplicity))
            logger.info('Setting multiplicity of reaction {0} to {1}'.format(self.label, self.multiplicity))

    def determine_rxn_charge(self):
        """A helper function for determining the surface charge"""
        if len(self.r_species):
            self.charge = sum([r.charge for r in self.r_species])

    def determine_family(self, rmgdatabase):
        """Determine the RMG family and saves the (family, own reverse) tuple in the ``family`` attribute"""
        if self.rmg_reaction is not None:
            self.family, self.family_own_reverse = rmgdb.determine_reaction_family(rmgdb=rmgdatabase,
                                                                                   reaction=self.rmg_reaction)

    def check_ts(self, log=True):
        """
        Check that the TS E0 is above both reactants and products wells
        Return ``False`` if this test fails, else ``True``
        """
        if any([spc.e_elect is None for spc in self.r_species + self.p_species + [self.ts_species]]):
            logger.error(f"Could not get E0's of all species in reaction {self.label}. Cannot check TS E0.")
            r_e_elect = None if any([spc.e_elect is None for spc in self.r_species])\
                else sum(spc.e_elect for spc in self.r_species)
            p_e_elect = None if any([spc.e_elect is None for spc in self.p_species])\
                else sum(spc.e_elect for spc in self.p_species)
            ts_e_elect = self.ts_species.e_elect
            logger.error(f'Reactants E0: {r_e_elect}\nProducts E0: {p_e_elect}\nTS E0: {ts_e_elect}')
            return True
        r_e_elect = sum([spc.e_elect for spc in self.r_species])
        p_e_elect = sum([spc.e_elect for spc in self.p_species])
        if self.ts_species.e_elect < r_e_elect or self.ts_species.e_elect < p_e_elect:
            if log:
                logger.error(f'TS of reaction {self.label} has a lower E0 value than expected:\n'
                             f'Reactants: {r_e_elect:.2f} kJ/mol\n'
                             f'TS: {self.ts_species.e_elect:.2f} kJ/mol'
                             f'\nProducts: {p_e_elect:.2f} kJ/mol')
            return False
        if log:
            logger.info(f'Reaction {self.label} has the following path energies:\n'
                        f'Reactants: {r_e_elect:.2f} kJ/mol\n'
                        f'TS: {self.ts_species.e_elect:.2f} kJ/mol\n'
                        f'Products: {p_e_elect:.2f} kJ/mol')
        return True

    def check_attributes(self):
        """Check that the Reaction object is defined correctly"""
        self.set_label_reactants_products()
        if not self.label:
            raise ReactionError('A reaction seems to not be defined correctly')
        if self.arrow not in self.label:
            raise ReactionError('A reaction label must include a double ended arrow with spaces on both'
                                ' sides: "{0}". Got:{1}'.format(self.arrow, self.label))
        if '+' in self.label and self.plus not in self.label:
            raise ReactionError('Reactants or products in a reaction label must separated with {0} (has spaces on both'
                                ' sides). Got:{1}'.format(self.plus, self.label))
        species_labels = self.label.split(self.arrow)
        reactants = species_labels[0].split(self.plus)
        products = species_labels[1].split(self.plus)
        if self.reactants is not None:
            for reactant in reactants:
                if reactant not in self.reactants:
                    raise ReactionError('Reactant {0} from the reaction label {1} not in self.reactants ({2})'.format(
                        reactant, self.label, self.reactants))
            for reactant in self.reactants:
                if reactant not in reactants:
                    raise ReactionError('Reactant {0} not in the reaction label ({1})'.format(reactant, self.label))
        if self.products is not None:
            for product in products:
                if product not in self.products:
                    raise ReactionError('Product {0} from the reaction label {1} not in self.products ({2})'.format(
                        product, self.label, self.products))
            for product in self.products:
                if product not in products:
                    raise ReactionError('Product {0} not in the reaction label ({1})'.format(product, self.label))
        if self.r_species is not None:
            for reactant in self.r_species:
                if reactant.label not in self.reactants:
                    raise ReactionError('Reactant {0} from not in self.reactants ({1})'.format(
                        reactant.label, self.reactants))
            for reactant in reactants:
                if reactant not in [r.label for r in self.r_species]:
                    raise ReactionError('Reactant {0} from the reaction label {1} not in self.r_species ({2})'.format(
                        reactant, self.label, [r.label for r in self.r_species]))
            for reactant in self.reactants:
                if reactant not in [r.label for r in self.r_species]:
                    raise ReactionError('Reactant {0} not in n self.r_species ({1})'.format(
                        reactant, [r.label for r in self.r_species]))
        if self.p_species is not None:
            for product in self.p_species:
                if product.label not in self.products:
                    raise ReactionError('Product {0} from not in self.products ({1})'.format(
                        product.label, self.reactants))
            for product in products:
                if product not in [p.label for p in self.p_species]:
                    raise ReactionError('Product {0} from the reaction label {1} not in self.p_species ({2})'.format(
                        product, self.label, [p.label for p in self.p_species]))
            for product in self.products:
                if product not in [p.label for p in self.p_species]:
                    raise ReactionError('Product {0} not in n self.p_species ({1})'.format(
                        product, [p.label for p in self.p_species]))
