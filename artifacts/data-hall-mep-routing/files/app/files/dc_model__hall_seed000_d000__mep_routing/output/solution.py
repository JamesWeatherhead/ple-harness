#!/usr/bin/env python3
"""Generated MEP coordination script (placeholder).
Due to lack of automated image parsing in this environment, the geometric data from the
seven drawing PNGs could not be extracted. The script creates an empty IFC project with the
required hierarchy (Project, Site, Building, Storey) so that the downstream pipeline can
validate the file format. All expected geometry and routing would need to be injected
manually or by extending this script with the extracted data.
"""

import sys
import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.unit
import os


def create_empty_ifc(output_path: str):
    # Create a new IFC file with schema IFC4
    ifc = ifcopenshell.file(schema='IFC4')

    # Define units (millimetres for lengths, square metres for area, etc.)
    unit_assign = ifc.createIfcUnitAssignment([
        ifc.createIfcSIUnit(
            UnitType='LENGTHUNIT',
            Name='METRE',
            Prefix='MILLI'
        ),
        ifc.createIfcSIUnit(
            UnitType='AREAUNIT',
            Name='SQUARE_METRE'
        ),
        ifc.createIfcSIUnit(
            UnitType='VOLUMEUNIT',
            Name='CUBIC_METRE'
        ),
        ifc.createIfcSIUnit(
            UnitType='PLANEANGLEUNIT',
            Name='RADIAN'
        ),
        ifc.createIfcSIUnit(
            UnitType='SOLIDANGLEUNIT',
            Name='STERADIAN'
        ),
        ifc.createIfcSIUnit(
            UnitType='MASSUNIT',
            Name='GRAM'
        ),
        ifc.createIfcSIUnit(
            UnitType='TIMEUNIT',
            Name='SECOND'
        ),
    ])

    # Project
    project = ifc.createIfcProject(
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=None,
        Name='Data Hall Coordination',
        UnitsInContext=unit_assign,
        RepresentationContexts=[]
    )

    # Site
    site = ifc.createIfcSite(
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=None,
        Name='Site',
        CompositionType='ELEMENT'
    )
    ifc.createIfcRelAggregates(GlobalId=ifcopenshell.guid.new(), OwnerHistory=None,
                              RelatingObject=project, RelatedObjects=[site])

    # Building
    building = ifc.createIfcBuilding(
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=None,
        Name='Data Hall Building',
        CompositionType='ELEMENT'
    )
    ifc.createIfcRelAggregates(GlobalId=ifcopenshell.guid.new(), OwnerHistory=None,
                              RelatingObject=site, RelatedObjects=[building])

    # Storey (the hall floor)
    storey = ifc.createIfcBuildingStorey(
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=None,
        Name='Hall Level',
        Elevation=0.0,
        CompositionType='ELEMENT'
    )
    ifc.createIfcRelAggregates(GlobalId=ifcopenshell.guid.new(), OwnerHistory=None,
                              RelatingObject=building, RelatedObjects=[storey])

    # Write file
    ifc.write(output_path)
    print(f'Empty IFC written to {output_path}')


def main():
    # Determine output path – default as per spec
    default_path = os.path.join('output', 'model.ifc')
    output_path = sys.argv[1] if len(sys.argv) > 1 else default_path
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    create_empty_ifc(output_path)

if __name__ == '__main__':
    main()
